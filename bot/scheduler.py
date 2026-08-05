import datetime
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.config import CHANNEL_ID, REMINDER_DAYS_BEFORE
from bot.database import async_session
from bot.models import Subscription, SubscriptionStatus, User, Order, OrderStatus
from bot.services import activate_subscription
from bot.payments import cryptobot

log = logging.getLogger(__name__)


async def check_reminders(bot: Bot):
    now = datetime.datetime.utcnow()
    threshold = now + datetime.timedelta(days=REMINDER_DAYS_BEFORE)

    async with async_session() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.reminded.is_(False),
                Subscription.end_date <= threshold,
                Subscription.end_date > now,
            )
        )
        subs = result.scalars().all()

        for sub in subs:
            user = await session.get(User, sub.user_id)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⭐ Продлить подписку", callback_data="subscribe_stars")]
                ]
            )
            try:
                await bot.send_message(
                    user.telegram_id,
                    f"Твоя подписка заканчивается {sub.end_date.strftime('%d.%m.%Y')}. "
                    "Продли, чтобы не потерять доступ к каналу.",
                    reply_markup=kb,
                )
                sub.reminded = True
            except Exception as e:
                log.warning(f"Не удалось отправить напоминание user_id={sub.user_id}: {e}")

        await session.commit()


async def check_expired(bot: Bot):
    now = datetime.datetime.utcnow()

    async with async_session() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.end_date <= now,
            )
        )
        subs = result.scalars().all()

        for sub in subs:
            user = await session.get(User, sub.user_id)
            try:
                # ban + unban = кикнуть, но оставить возможность зайти заново по новой ссылке
                await bot.ban_chat_member(CHANNEL_ID, user.telegram_id)
                await bot.unban_chat_member(CHANNEL_ID, user.telegram_id)
                await bot.send_message(
                    user.telegram_id,
                    "Подписка закончилась, доступ к каналу закрыт. "
                    "Оформи новую подписку через /start, чтобы вернуться.",
                )
            except Exception as e:
                log.warning(f"Не удалось кикнуть user_id={sub.user_id}: {e}")
            finally:
                sub.status = SubscriptionStatus.expired

        await session.commit()


async def check_crypto_payments(bot: Bot):
    """Опрашивает CryptoBot по всем незавершённым крипто-заказам.
    Временное решение для локальной разработки без публичного домена —
    на проде это заменит webhook от CryptoBot."""
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.method == "crypto", Order.status == OrderStatus.pending)
        )
        orders = result.scalars().all()

        for order in orders:
            if not order.telegram_charge_id or not order.telegram_charge_id.startswith("cryptobot_"):
                continue
            invoice_id = int(order.telegram_charge_id.removeprefix("cryptobot_"))

            status = await cryptobot.get_invoice_status(invoice_id)
            if status != "paid":
                continue  # ещё не оплачен или просрочен — просто ждём

            order.status = OrderStatus.paid
            user = await session.get(User, order.user_id)
            invite_link = await activate_subscription(session, bot, user)

            try:
                await bot.send_message(
                    user.telegram_id,
                    "Оплата получена ✅\n\n"
                    f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link}",
                )
            except Exception as e:
                log.warning(f"Не удалось отправить ссылку user_id={user.id}: {e}")

        await session.commit()


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", hours=1, args=[bot])
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_crypto_payments, "interval", seconds=30, args=[bot])
    return scheduler
