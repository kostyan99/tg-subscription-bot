import datetime
import logging
import secrets

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from bot.config import (
    SUBSCRIPTION_PRICE_STARS,
    SUBSCRIPTION_DAYS,
    SUBSCRIPTION_PRICE_USDT,
    SUBSCRIPTION_PRICE_USDT_CENTS,
    ADMIN_TELEGRAM_ID,
    CHANNEL_ID,
    MIN_WITHDRAWAL_USDT_CENTS,
)
from bot.database import async_session
from bot.models import User, Order, OrderStatus, ReferralEarning, Subscription, SubscriptionStatus
from bot.services import (
    activate_subscription,
    record_referral_earning,
    process_crypto_withdrawal,
    InviteLinkError,
)
from bot.payments import cryptobot
from bot import keyboards

router = Router()
log = logging.getLogger(__name__)

REFERRALS_PAGE_SIZE = 5

# Простой троттлинг на клик "оплатить криптой" — защита от спам-кликов,
# каждый из которых бьёт по внешнему CryptoBot API. In-memory, живёт, пока
# жив процесс — этого достаточно для одного инстанса бота.
_last_crypto_click: dict[int, datetime.datetime] = {}
CRYPTO_CLICK_COOLDOWN = datetime.timedelta(seconds=5)
# Если у юзера уже есть неоплаченный крипто-заказ моложе этого времени —
# не создаём новый инвойс, а переиспользуем существующий
CRYPTO_ORDER_REUSE_WINDOW = datetime.timedelta(minutes=10)

# Защита от двойного клика "Подтвердить вывод" — без неё два быстрых клика
# могут пройти проверку "нет активной заявки" ДО того, как первый успеет
# закоммититься, и создать два РЕАЛЬНЫХ перевода в CryptoBot одновременно
_withdrawal_in_progress: set[int] = set()


async def get_or_create_user(session, tg_user, referral_code: str | None = None) -> User:
    result = await session.execute(
        select(User).where(User.telegram_id == tg_user.id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    referred_by_id = None
    if referral_code and len(referral_code) <= 24:  # наш формат кода — 16 символов, но не доверяем длине от юзера
        result = await session.execute(
            select(User).where(User.referral_code == referral_code)
        )
        referrer = result.scalar_one_or_none()
        if referrer is not None:
            referred_by_id = referrer.id

    # token_hex(8) = 16 hex-символов = 2^64 комбинаций — при таком пространстве
    # коллизия практически невозможна даже на миллионах юзеров (в отличие от
    # 8-символьного варианта, где на 100k юзеров шанс коллизии уже ~69%).
    # Retry-цикл — подстраховка на всякий случай, а не основная защита.
    for attempt in range(5):
        try:
            user = User(
                telegram_id=tg_user.id,
                username=tg_user.username,
                referral_code=secrets.token_hex(8),
                referred_by_id=referred_by_id,
            )
            session.add(user)
            await session.flush()  # чтобы получить user.id до коммита; тут же поймаем коллизию
            return user
        except IntegrityError:
            await session.rollback()
            # Коллизия может быть по двум разным причинам: (а) редчайшее совпадение
            # referral_code — тогда просто пробуем новый; (б) два ПАРАЛЛЕЛЬНЫХ /start
            # от одного юзера успели создать дубликат по telegram_id — тогда retry
            # с новым кодом ничего не исправит, будет падать точно так же снова.
            # Проверяем — если юзер уже существует (создан параллельным запросом),
            # просто возвращаем его вместо бессмысленных повторных попыток.
            result = await session.execute(
                select(User).where(User.telegram_id == tg_user.id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing
            if attempt == 4:
                raise
    raise RuntimeError("unreachable")  # для линтера — цикл выше либо вернёт, либо бросит


# ---------- Главное меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    async with async_session() as session:
        await get_or_create_user(session, message.from_user, referral_code=command.args)
        await session.commit()

    await message.answer(
        "Привет! Выбери раздел:",
        reply_markup=keyboards.main_menu_kb(),
    )


@router.callback_query(F.data == "menu_main")
async def menu_main(callback: CallbackQuery):
    await callback.message.edit_text(
        "Привет! Выбери раздел:",
        reply_markup=keyboards.main_menu_kb(),
    )
    await callback.answer()


# ---------- Раздел "Подписка" ----------

@router.callback_query(F.data == "menu_subscription")
async def menu_subscription(callback: CallbackQuery):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()

        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = result.scalar_one_or_none()

    now = datetime.datetime.utcnow()
    if sub and sub.status == SubscriptionStatus.active and sub.end_date > now:
        status_line = f"✅ Подписка активна до {sub.end_date.strftime('%d.%m.%Y')}"
    else:
        status_line = "❌ Нет активной подписки"

    await callback.message.edit_text(
        f"{status_line}\n\n"
        f"Стоимость: {SUBSCRIPTION_PRICE_STARS} Stars или {SUBSCRIPTION_PRICE_USDT} USDT "
        f"за {SUBSCRIPTION_DAYS} дней.",
        reply_markup=keyboards.subscription_menu_kb(),
    )
    await callback.answer()


# ---------- Раздел "Реферальная программа" ----------

@router.callback_query(F.data == "menu_referral")
async def menu_referral(callback: CallbackQuery, bot: Bot):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()

        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={user.referral_code}"

        result = await session.execute(
            select(func.count(User.id)).where(User.referred_by_id == user.id)
        )
        referred_count = result.scalar_one()

        result = await session.execute(
            select(ReferralEarning.method, func.sum(ReferralEarning.amount))
            .where(ReferralEarning.referrer_id == user.id)
            .group_by(ReferralEarning.method)
        )
        earnings_by_method = result.all()

    lines = [f"🔗 Твоя реферальная ссылка:\n{link}", "", f"Приглашено: {referred_count}"]
    if earnings_by_method:
        lines.append("\nЗаработано всего:")
        for method, total in earnings_by_method:
            if method == "stars":
                lines.append(f"⭐ {total} Stars")
            elif method == "crypto":
                lines.append(f"₿ {total / 100:.2f} USDT")
            else:
                lines.append(f"{method}: {total}")
    else:
        lines.append("\nПока никто не оплатил по твоей ссылке.")

    await callback.message.edit_text("\n".join(lines), reply_markup=keyboards.referral_menu_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("referral_list_"))
async def referral_list(callback: CallbackQuery):
    page = int(callback.data.removeprefix("referral_list_"))

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()

        result = await session.execute(
            select(User)
            .where(User.referred_by_id == user.id)
            .order_by(User.created_at.desc())
            .offset(page * REFERRALS_PAGE_SIZE)
            .limit(REFERRALS_PAGE_SIZE + 1)  # +1 чтобы понять, есть ли следующая страница
        )
        referred_users = result.scalars().all()
        has_next = len(referred_users) > REFERRALS_PAGE_SIZE
        referred_users = referred_users[:REFERRALS_PAGE_SIZE]

        lines = [f"👥 Твои рефералы (стр. {page + 1}):\n"]
        if not referred_users:
            lines.append("Пока никого нет." if page == 0 else "Больше никого нет.")

        for ref_user in referred_users:
            name = f"@{ref_user.username}" if ref_user.username else f"id{ref_user.telegram_id}"
            joined = ref_user.created_at.strftime("%d.%m.%Y")

            result2 = await session.execute(
                select(ReferralEarning.method, func.sum(ReferralEarning.amount))
                .where(
                    ReferralEarning.referrer_id == user.id,
                    ReferralEarning.referred_user_id == ref_user.id,
                )
                .group_by(ReferralEarning.method)
            )
            earnings = result2.all()
            if earnings:
                earn_str = ", ".join(
                    f"{total} Stars" if method == "stars" else f"{total / 100:.2f} USDT"
                    for method, total in earnings
                )
                lines.append(f"• {name} — с {joined}, оплатил ({earn_str})")
            else:
                lines.append(f"• {name} — с {joined}, ещё не оплатил")

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=keyboards.referral_list_kb(page, has_next)
    )
    await callback.answer()


@router.callback_query(F.data == "withdraw_start")
async def withdraw_start(callback: CallbackQuery):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()

        result = await session.execute(
            select(func.sum(ReferralEarning.amount)).where(
                ReferralEarning.referrer_id == user.id,
                ReferralEarning.method == "crypto",
                ReferralEarning.paid_out.is_(False),
            )
        )
        total = result.scalar_one() or 0

    if total < MIN_WITHDRAWAL_USDT_CENTS:
        await callback.message.edit_text(
            f"Пока накоплено {total / 100:.2f} USDT из начислений в крипте.\n"
            f"Минимум для вывода — {MIN_WITHDRAWAL_USDT_CENTS / 100:.2f} USDT.\n\n"
            "⭐ Начисления в Stars выводятся не автоматически — по ним свяжись "
            "с администратором отдельно, программно перевести звёзды нельзя.",
            reply_markup=keyboards.back_kb("menu_referral"),
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"К выводу: {total / 100:.2f} USDT\n\n"
        "Деньги придут на твой баланс в @CryptoBot. Если ты никогда не открывал "
        "этого бота — сначала напиши ему /start, иначе перевод не пройдёт.\n\n"
        "Подтвердить вывод?",
        reply_markup=keyboards.withdraw_confirm_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "withdraw_execute")
async def withdraw_execute(callback: CallbackQuery, bot: Bot):
    user_tg_id = callback.from_user.id

    if user_tg_id in _withdrawal_in_progress:
        await callback.answer("Заявка уже обрабатывается, подожди пару секунд", show_alert=False)
        return
    _withdrawal_in_progress.add(user_tg_id)

    try:
        await callback.answer("Обрабатываю перевод...")

        async with async_session() as session:
            user = await get_or_create_user(session, callback.from_user)
            await session.commit()

            success, text = await process_crypto_withdrawal(session, bot, user)

        await callback.message.edit_text(text, reply_markup=keyboards.back_kb("menu_referral"))
    finally:
        _withdrawal_in_progress.discard(user_tg_id)


# ---------- Админ-команды ----------

@router.message(Command("referral_report"))
async def referral_report(message: Message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return  # молча игнорируем — не раскрываем, что команда вообще существует

    async with async_session() as session:
        result = await session.execute(
            select(
                User.id,
                User.telegram_id,
                User.username,
                ReferralEarning.method,
                func.sum(ReferralEarning.amount),
            )
            .join(ReferralEarning, ReferralEarning.referrer_id == User.id)
            .where(ReferralEarning.paid_out.is_(False))
            .group_by(User.id, ReferralEarning.method)
        )
        rows = result.all()

    if not rows:
        await message.answer("Невыплаченных начислений нет.")
        return

    lines = ["💰 К выплате рефererам:\n"]
    for user_id, telegram_id, username, method, total in rows:
        name = f"@{username}" if username else str(telegram_id)
        amount_str = f"{total} Stars" if method == "stars" else f"{total / 100:.2f} USDT"
        lines.append(f"{name} (id={telegram_id}): {amount_str}")

    lines.append(
        "\nПосле того как выплатишь вручную — отметь так:\n"
        "/mark_paid <telegram_id>"
    )
    await message.answer("\n".join(lines))


@router.message(Command("mark_paid"))
async def mark_paid(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /mark_paid <telegram_id>")
        return

    target_telegram_id = int(command.args.strip())

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == target_telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Такого пользователя нет в базе.")
            return

        result = await session.execute(
            select(ReferralEarning).where(
                ReferralEarning.referrer_id == user.id,
                ReferralEarning.paid_out.is_(False),
            )
        )
        earnings = result.scalars().all()
        for earning in earnings:
            earning.paid_out = True
        await session.commit()

    await message.answer(f"Отмечено как выплачено: {len(earnings)} начислений.")


@router.message(Command("resend_invite"))
async def resend_invite(message: Message, command: CommandObject, bot: Bot):
    """Ручной аварийный резерв: если activate_subscription не смог создать ссылку
    (бот временно терял права в канале и т.п.), админ чинит права и вызывает эту
    команду — она заново генерирует ссылку тому, у кого уже активная подписка в БД."""
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /resend_invite <telegram_id>")
        return

    target_telegram_id = int(command.args.strip())

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == target_telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Такого пользователя нет в базе.")
            return

        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = result.scalar_one_or_none()
        if sub is None or sub.status != SubscriptionStatus.active:
            await message.answer("У этого юзера нет активной подписки в БД.")
            return

    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=int((datetime.datetime.utcnow() + datetime.timedelta(days=1)).timestamp()),
        )
    except TelegramAPIError as e:
        await message.answer(f"Всё ещё не получается создать ссылку: {e}")
        return

    try:
        await bot.send_message(
            target_telegram_id,
            f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link.invite_link}",
        )
        await message.answer("Ссылка создана и отправлена юзеру.")
    except TelegramAPIError as e:
        await message.answer(f"Ссылка создана, но не смог отправить юзеру: {e}")


# ---------- Оплата Stars ----------

@router.callback_query(F.data == "subscribe_stars")
async def subscribe_stars(callback: CallbackQuery, bot: Bot):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка на закрытый канал",
        description=f"Доступ на {SUBSCRIPTION_DAYS} дней",
        payload=f"sub_{callback.from_user.id}_{datetime.datetime.utcnow().timestamp()}",
        provider_token="",  # для Stars всегда пустая строка
        currency="XTR",
        prices=[LabeledPrice(label="Подписка", amount=SUBSCRIPTION_PRICE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_q: PreCheckoutQuery):
    # Обязаны ответить в течение 10 секунд, иначе Telegram отменит платёж.
    await pre_checkout_q.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)

        # Защита от повторной обработки одного и того же платежа.
        # Проверка "есть ли уже такой charge_id" плюс уникальный constraint
        # в БД на этом же поле — двойная защита: constraint ловит редкую гонку,
        # если два апдейта пришли почти одновременно и проверка не успела увидеть
        # запись друг друга.
        existing = await session.execute(
            select(Order).where(Order.telegram_charge_id == payment.telegram_payment_charge_id)
        )
        if existing.scalar_one_or_none() is not None:
            return

        order = Order(
            user_id=user.id,
            method="stars",
            amount=payment.total_amount,
            status=OrderStatus.paid,
            telegram_charge_id=payment.telegram_payment_charge_id,
        )
        session.add(order)
        try:
            await session.commit()
        except IntegrityError:
            # Параллельный апдейт уже успел создать заказ с этим charge_id первым —
            # платёж уже обрабатывается/обработан, тут делать больше нечего
            await session.rollback()
            return

        await record_referral_earning(session, order, user)

        try:
            invite_link = await activate_subscription(session, bot, user)
        except InviteLinkError:
            await message.answer(
                "Оплата прошла ✅, но возникла техническая проблема при выдаче доступа. "
                "Мы уже разбираемся, ссылку пришлём отдельным сообщением в ближайшее время."
            )
            return

    if invite_link:
        await message.answer(
            "Оплата прошла ✅\n\n"
            f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link}"
        )
    else:
        await message.answer(
            "Оплата прошла ✅ Подписка продлена — ты уже в канале, никаких доп. действий не нужно."
        )


# ---------- Оплата криптой ----------

@router.callback_query(F.data == "subscribe_crypto")
async def subscribe_crypto(callback: CallbackQuery, bot: Bot):
    now = datetime.datetime.utcnow()
    last_click = _last_crypto_click.get(callback.from_user.id)
    if last_click and now - last_click < CRYPTO_CLICK_COOLDOWN:
        await callback.answer("Не так быстро — подожди пару секунд", show_alert=False)
        return
    _last_crypto_click[callback.from_user.id] = now

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        await session.commit()

        # Если у юзера уже есть недавний неоплаченный крипто-заказ — не плодим
        # новый инвойс на каждый клик, переиспользуем существующий
        result = await session.execute(
            select(Order)
            .where(
                Order.user_id == user.id,
                Order.method == "crypto",
                Order.status == OrderStatus.pending,
                Order.created_at > now - CRYPTO_ORDER_REUSE_WINDOW,
            )
            .order_by(Order.created_at.desc())
        )
        existing_order = result.scalars().first()

    if existing_order and existing_order.telegram_charge_id:
        invoice_id = int(existing_order.telegram_charge_id.removeprefix("cryptobot_"))
        statuses = await cryptobot.get_invoices_statuses([invoice_id])
        if statuses.get(invoice_id) == "active":
            await callback.message.answer(
                "У тебя уже есть неоплаченный счёт — используй его, новый создавать не нужно.\n"
                "Если ссылка потерялась, напиши в поддержку."
            )
            await callback.answer()
            return
        # иначе (expired/не найден) — просто создаём новый ниже, старый останется в истории как есть

    try:
        invoice = await cryptobot.create_invoice(
            amount=SUBSCRIPTION_PRICE_USDT,
            asset="USDT",
            payload="",  # заполним order_id после создания заказа
            description="Подписка на закрытый канал",
        )
    except cryptobot.CryptoBotError:
        await callback.message.answer(
            "Не получилось создать счёт для оплаты, попробуй чуть позже."
        )
        await callback.answer()
        return

    async with async_session() as session:
        order = Order(
            user_id=user.id,
            method="crypto",
            amount=SUBSCRIPTION_PRICE_USDT_CENTS,
            status=OrderStatus.pending,
            telegram_charge_id=f"cryptobot_{invoice['invoice_id']}",
        )
        session.add(order)
        await session.commit()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=invoice["pay_url"])]
        ]
    )
    await callback.message.answer(
        f"Счёт на {SUBSCRIPTION_PRICE_USDT} USDT создан.\n"
        "После оплаты доступ откроется автоматически, обычно в течение минуты.",
        reply_markup=kb,
    )
    await callback.answer()
