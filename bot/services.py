import datetime
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import (
    CHANNEL_ID,
    SUBSCRIPTION_DAYS,
    REFERRAL_PERCENT,
    ADMIN_TELEGRAM_ID,
    MIN_WITHDRAWAL_USDT_CENTS,
)
from bot.models import (
    Subscription,
    SubscriptionStatus,
    User,
    Order,
    ReferralEarning,
    Withdrawal,
    WithdrawalStatus,
)
from bot.payments import cryptobot

log = logging.getLogger(__name__)


class InviteLinkError(Exception):
    """Подписка успешно активирована в БД (деньги не потеряны), но создать
    invite-ссылку не удалось — например, бот потерял права админа в канале.
    Вызывающий код должен поймать это и сообщить юзеру мягко, без паники,
    а не просто уронить хендлер."""


async def activate_subscription(session: AsyncSession, bot: Bot, user: User) -> str | None:
    """Продлевает/создаёт подписку. Возвращает одноразовую invite-ссылку —
    но ТОЛЬКО если юзера ещё нет в канале. Если это продление и юзер уже
    состоит в канале, возвращает None: новая ссылка ему не нужна для входа,
    а раз она одноразовая (member_limit=1) — при выдаче "про запас" он мог бы
    просто передать её другу для бесплатного доступа мимо оплаты.
    Вызывается ПОСЛЕ того, как оплата уже подтверждена (неважно, каким способом).
    Может бросить InviteLinkError — это ловится отдельно от прочих ошибок."""

    now = datetime.datetime.utcnow()
    result = await session.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    sub = result.scalar_one_or_none()

    base_date = sub.end_date if (sub and sub.end_date > now) else now
    new_end = base_date + datetime.timedelta(days=SUBSCRIPTION_DAYS)

    if sub is None:
        sub = Subscription(
            user_id=user.id,
            start_date=now,
            end_date=new_end,
            status=SubscriptionStatus.active,
            reminded=False,
        )
        session.add(sub)
    else:
        sub.end_date = new_end
        sub.status = SubscriptionStatus.active
        sub.reminded = False

    # Коммитим подписку ДО попытки создать ссылку — так подписка гарантированно
    # активна в БД, даже если следующий шаг упадёт. Юзер не теряет оплаченные дни
    # из-за технической проблемы с созданием ссылки.
    await session.commit()

    try:
        member = await bot.get_chat_member(CHANNEL_ID, user.telegram_id)
        already_in_channel = member.status not in ("left", "kicked")
    except Exception as e:
        # Намеренно ловим ЛЮБОЕ исключение, не только TelegramAPIError — сюда
        # же попадут сетевые сбои, таймауты и т.п. Не отказываем в ссылке
        # из-за технического сбоя проверки, лучше выдать лишнюю ссылку, чем
        # оставить реально нового юзера без доступа
        log.warning(f"Не удалось проверить членство в канале для user_id={user.id}: {e}")
        already_in_channel = False

    if already_in_channel:
        return None

    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=int((now + datetime.timedelta(days=1)).timestamp()),
        )
        return invite_link.invite_link
    except TelegramAPIError as e:
        log.critical(f"Не удалось создать invite-ссылку для user_id={user.id}: {e}")
        if ADMIN_TELEGRAM_ID:
            try:
                await bot.send_message(
                    ADMIN_TELEGRAM_ID,
                    "⚠️ Оплата прошла и подписка активирована в БД, но не удалось "
                    f"создать ссылку для user_id={user.id} (telegram_id={user.telegram_id}).\n"
                    "Проверь, что бот всё ещё админ канала с правом приглашать по ссылке, "
                    f"затем вызови /resend_invite {user.telegram_id}",
                )
            except TelegramAPIError:
                pass  # если даже админу не достучаться — хотя бы лог остался
        raise InviteLinkError from e


async def record_referral_earning(session: AsyncSession, order: Order, user: User) -> None:
    """Если юзера кто-то пригласил — начисляет пригласившему REFERRAL_PERCENT
    от суммы заказа. Вызывать сразу после того, как order помечен paid."""
    if user.referred_by_id is None:
        return

    referrer = await session.get(User, user.referred_by_id)
    if referrer is None:
        return

    earning = ReferralEarning(
        referrer_id=referrer.id,
        referred_user_id=user.id,
        order_id=order.id,
        method=order.method,
        amount=round(order.amount * REFERRAL_PERCENT / 100),
    )
    session.add(earning)
    await session.commit()


async def process_crypto_withdrawal(session: AsyncSession, user: User) -> tuple[bool, str]:
    """Выводит накопленные крипто-начисления реферера на его баланс в CryptoBot.
    Возвращает (успех, сообщение_для_юзера). Работает ТОЛЬКО с method='crypto' —
    Stars так вывести нельзя, у Bot API просто нет такого метода."""

    # Не даём запустить вторую заявку, пока предыдущая ещё не завершилась —
    # без этой проверки два быстрых нажатия кнопки могли бы создать два перевода
    result = await session.execute(
        select(Withdrawal).where(
            Withdrawal.user_id == user.id, Withdrawal.status == WithdrawalStatus.pending
        )
    )
    if result.scalar_one_or_none() is not None:
        return False, "У тебя уже есть заявка на вывод в обработке, подожди её завершения."

    result = await session.execute(
        select(ReferralEarning).where(
            ReferralEarning.referrer_id == user.id,
            ReferralEarning.method == "crypto",
            ReferralEarning.paid_out.is_(False),
        )
    )
    earnings = result.scalars().all()
    total = sum(e.amount for e in earnings)

    if total < MIN_WITHDRAWAL_USDT_CENTS:
        return False, (
            f"Пока недостаточно для вывода: накоплено {total / 100:.2f} USDT, "
            f"минимум для вывода — {MIN_WITHDRAWAL_USDT_CENTS / 100:.2f} USDT."
        )

    withdrawal = Withdrawal(
        user_id=user.id,
        method="crypto",
        amount=total,
        status=WithdrawalStatus.pending,
    )
    session.add(withdrawal)
    await session.flush()  # получаем withdrawal.id для spend_id, ещё не коммитим как completed
    await session.commit()

    earning_ids = [e.id for e in earnings]

    try:
        # Не передаём comment по умолчанию — для новых приложений CryptoBot запрещает
        # прикреплять комментарий, из‑за чего запрос падает (CANNOT_ATTACH_COMMENT).
        transfer_result = await cryptobot.transfer(
            user_id=user.telegram_id,
            asset="USDT",
            amount=f"{total / 100:.2f}",
            spend_id=f"ref_withdrawal_{withdrawal.id}",
        )
    except cryptobot.CryptoBotError as e:
        # Сохраняем сырый ответ в error_message для диагностики
        raw = getattr(e, "data", str(e))
        withdrawal.status = WithdrawalStatus.failed
        try:
            withdrawal.error_message = str(raw)[:500]
        except Exception:
            withdrawal.error_message = "(error while serializing error)"
        await session.commit()

        # Попробуем распарсить имя ошибки, если CryptoBot вернул структуру {"error": {"name": ...}}
        err_name = None
        try:
            if isinstance(raw, dict):
                err_name = raw.get("error", {}).get("name")
        except Exception:
            err_name = None

        if err_name == "INSUFFICIENT_FUNDS":
            log.error(f"INSUFFICIENT_FUNDS during transfer spend_id=ref_withdrawal_{withdrawal.id}: {raw}")
            # Не пытаемся автопополнять баланс — просто информируем пользователя
            return False, (
                "Не получилось выполнить перевод: на балансе приложения недостаточно средств. "
                "Пожалуйста, свяжись с администратором или попробуй позже."
            )

        # Общая ошибка: скорее всего пользователь не написал /start боту CryptoBot
        log.error(f"CryptoBot transfer failed for user_id={user.telegram_id}, spend_id=ref_withdrawal_{withdrawal.id}: {raw}")
        return False, (
            "Не получилось выполнить перевод. Самая частая причина — ты ни разу "
            "не открывал @CryptoBot. Напиши ему /start и попробуй вывести ещё раз."
        )

    # Если всё успешно — отмечаем
    withdrawal.status = WithdrawalStatus.completed
    withdrawal.cryptobot_transfer_id = str(transfer_result.get("transfer_id", ""))
    await session.execute(
        update(ReferralEarning).where(ReferralEarning.id.in_(earning_ids)).values(paid_out=True)
    )
    await session.commit()

    return True, f"✅ Выведено {total / 100:.2f} USDT на твой баланс в CryptoBot."
