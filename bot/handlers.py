import datetime
import logging

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select

from bot.config import SUBSCRIPTION_PRICE_STARS, SUBSCRIPTION_DAYS, SUBSCRIPTION_PRICE_USDT
from bot.database import async_session
from bot.models import User, Order, OrderStatus
from bot.services import activate_subscription
from bot.payments import cryptobot

router = Router()
log = logging.getLogger(__name__)


async def get_or_create_user(session, tg_user) -> User:
    result = await session.execute(
        select(User).where(User.telegram_id == tg_user.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=tg_user.id, username=tg_user.username)
        session.add(user)
        await session.flush()  # чтобы получить user.id до коммита
    return user


@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ Оплатить {SUBSCRIPTION_PRICE_STARS} Stars",
                    callback_data="subscribe_stars",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"₿ Оплатить {SUBSCRIPTION_PRICE_USDT} USDT",
                    callback_data="subscribe_crypto",
                )
            ],
        ]
    )
    await message.answer(
        "Привет! Здесь можно оформить подписку на закрытый канал.\n\n"
        f"Стоимость: {SUBSCRIPTION_PRICE_STARS} Stars за {SUBSCRIPTION_DAYS} дней.",
        reply_markup=kb,
    )


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

        invite_link = await activate_subscription(session, bot, user)

    await message.answer(
        "Оплата прошла ✅\n\n"
        f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link}"
    )


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
