import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers import router
from bot.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO)


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = setup_scheduler(bot)
    scheduler.start()

    # Polling — для локальной разработки. На Railway позже переключим на webhook.
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
