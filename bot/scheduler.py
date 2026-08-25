import asyncio
import datetime
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from bot.config import CHANNEL_ID, REMINDER_DAYS_BEFORE
from bot.database import async_session
from bot.models import Subscription, SubscriptionStatus, User, Order, OrderStatus
from bot.services import activate_subscription, record_referral_earning, InviteLinkError
from bot.payments import cryptobot

log = logging.getLogger(__name__)

# Сколько держим неоплаченный крипто-заказ, прежде чем считать его протухшим
# и перестать проверять (иначе pending-заказы копятся в БД вечно)
CRYPTO_ORDER_TIMEOUT = datetime.timedelta(hours=1)


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

            # Пауза между отправками — при сотнях юзеров разом не словим
            # flood-control от Telegram (лимит примерно 20-30 сообщений/сек)
            await asyncio.sleep(0.05)

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
            except Exception as e:
                # НЕ помечаем expired при сбое кика — иначе при временной ошибке
                # (flood-control, сбой Telegram API) юзер навсегда останется
                # в канале: check_expired ищет только status == active, а если
                # мы тут поставим expired, повторной попытки уже не будет.
                # Оставляем active — просто попробуем ещё раз через час.
                log.warning(f"Не удалось кикнуть user_id={sub.user_id}: {e} — повторим в следующем цикле")
                await asyncio.sleep(0.05)
                continue

            sub.status = SubscriptionStatus.expired

            try:
                await bot.send_message(
                    user.telegram_id,
                    "Подписка закончилась, доступ к каналу закрыт. "
                    "Оформи новую подписку через /start, чтобы вернуться.",
                )
            except Exception as e:
                # Кик уже прошёл успешно — это главное. Уведомление необязательно,
                # не откатываем из-за него expired-статус.
                log.warning(f"Кикнули, но не смогли уведомить user_id={sub.user_id}: {e}")

            await asyncio.sleep(0.05)

        await session.commit()


async def check_crypto_payments(bot: Bot):
    """Опрашивает CryptoBot ОДНИМ батч-запросом по всем незавершённым крипто-заказам.
    Временное решение для локальной разработки без публичного домена —
    на проде это заменит webhook от CryptoBot."""
    async with async_session() as session:
        result = await session.execute(
            select(Order).where(Order.method == "crypto", Order.status == OrderStatus.pending)
        )
        orders = result.scalars().all()
        if not orders:
            return

        id_to_order: dict[int, Order] = {}
        for order in orders:
            if order.telegram_charge_id and order.telegram_charge_id.startswith("cryptobot_"):
                invoice_id = int(order.telegram_charge_id.removeprefix("cryptobot_"))
                id_to_order[invoice_id] = order

        statuses = await cryptobot.get_invoices_statuses(list(id_to_order.keys()))
        now = datetime.datetime.utcnow()

        for invoice_id, order in id_to_order.items():
            status = statuses.get(invoice_id)

            if status == "paid":
                # Каждый заказ обрабатываем в своём try/except — падение на одном
                # заказе не должно останавливать обработку остальных в этой же партии
                try:
                    order.status = OrderStatus.paid
                    user = await session.get(User, order.user_id)
                    await record_referral_earning(session, order, user)

                    try:
                        invite_link = await activate_subscription(session, bot, user)
                    except InviteLinkError:
                        # деньги и подписка в порядке, юзеру отдельно сообщим,
                        # что со ссылкой проблема — админ уже уведомлён внутри activate_subscription
                        await bot.send_message(
                            user.telegram_id,
                            "Оплата получена ✅, но возникла техническая проблема при "
                            "выдаче ссылки на канал. Мы уже разбираемся, напишем отдельно.",
                        )
                        continue

                    if invite_link:
                        await bot.send_message(
                            user.telegram_id,
                            "Оплата получена ✅\n\n"
                            f"Вот твоя ссылка на канал (одноразовая, действует 24 часа):\n{invite_link}",
                        )
                    else:
                        await bot.send_message(
                            user.telegram_id,
                            "Оплата получена ✅ Подписка продлена — ты уже в канале, "
                            "никаких доп. действий не нужно.",
                        )
                except Exception as e:
                    log.error(f"Ошибка обработки crypto-заказа {order.id}: {e}")

            elif status == "expired":
                order.status = OrderStatus.failed

            elif status is None and (now - order.created_at) > CRYPTO_ORDER_TIMEOUT:
                # Инвойс не нашёлся в ответе И заказ уже старый — считаем протухшим,
                # чтобы не проверять его бесконечно
                order.status = OrderStatus.failed

        await session.commit()


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    # max_instances=1 (явно, хоть это и дефолт APScheduler) — гарантирует,
    # что новый запуск задачи не стартует, пока не закончился предыдущий.
    # Без этого при подвисшем HTTP-запросе к CryptoBot могли бы наложиться
    # два параллельных прогона check_crypto_payments и задвоить обработку заказа.
    scheduler.add_job(check_reminders, "interval", hours=1, args=[bot], max_instances=1)
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot], max_instances=1)
    scheduler.add_job(check_crypto_payments, "interval", seconds=30, args=[bot], max_instances=1)
    return scheduler
