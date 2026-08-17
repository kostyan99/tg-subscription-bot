import datetime
import logging
import secrets

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from bot.config import (
    SUBSCRIPTION_PRICE_STARS,
    SUBSCRIPTION_DAYS,
    SUBSCRIPTION_PRICE_USDT,
    ADMIN_TELEGRAM_ID,
)
from bot.database import async_session
from bot.models import User, Order, OrderStatus, ReferralEarning, Subscription, SubscriptionStatus
from bot.services import activate_subscription, record_referral_earning
from bot.payments import cryptobot
from bot import keyboards

router = Router()
log = logging.getLogger(__name__)

REFERRALS_PAGE_SIZE = 5


async def get_or_create_user(session, tg_user, referral_code: str | None = None) -> User:
    result = await session.execute(
        select(User).where(User.telegram_id == tg_user.id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    referred_by_id = None
    if referral_code:
        result = await session.execute(
            select(User).where(User.referral_code == referral_code)
        )
        referrer = result.scalar_one_or_none()
        if referrer is not None:
            referred_by_id = referrer.id

    user = User(
        telegram_id=tg_user.id,
        username=tg_user.username,
        referral_code=secrets.token_hex(4),  # 8 символов, годится для deep-link
        referred_by_id=referred_by_id,
    )
    session.add(user)
    await session.flush()  # чтобы получить user.id до коммита
    return user


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
    # Здесь можно добавить доп. проверки (например, не забанен ли юзер) —
    # пока просто подтверждаем.
    await pre_checkout_q.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)

        # Защита от повторной обработки одного и того же платежа
        existing = await session.execute(
            select(Order).where(Order.telegram_charge_id == payment.telegram_payment_charge_id)
        )
        if existing.scalar_one_or_none() is not None:
            await session.commit()
            return

        order = Order(
            user_id=user.id,
            method="stars",
            amount=payment.total_amount,
            status=OrderStatus.paid,
            telegram_charge_id=payment.telegram_payment_charge_id,
        )
        session.add(order)
        await session.commit()

        await record_referral_earning(session, order, user)
        invite_link = await activate_subscription(session, bot, user)

    await message.answer(
        "Оплата прошла ✅\n\n"
        f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link}"
    )


# ---------- Оплата криптой ----------

@router.callback_query(F.data == "subscribe_crypto")
async def subscribe_crypto(callback: CallbackQuery, bot: Bot):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        order = Order(
            user_id=user.id,
            method="crypto",
            amount=int(float(SUBSCRIPTION_PRICE_USDT) * 100),  # храним в центах
            status=OrderStatus.pending,
        )
        session.add(order)
        await session.flush()
        order_id = order.id
        await session.commit()

    try:
        invoice = await cryptobot.create_invoice(
            amount=SUBSCRIPTION_PRICE_USDT,
            asset="USDT",
            payload=order_id,
            description="Подписка на закрытый канал",
        )
    except cryptobot.CryptoBotError:
        await callback.message.answer(
            "Не получилось создать счёт для оплаты, попробуй чуть позже."
        )
        await callback.answer()
        return

    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one()
        # переиспользуем поле telegram_charge_id, чтобы хранить ID инвойса CryptoBot
        order.telegram_charge_id = f"cryptobot_{invoice['invoice_id']}"
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
