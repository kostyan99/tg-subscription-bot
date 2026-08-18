import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ErrorEvent

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers import router
from bot.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    @dp.error()
    async def global_error_handler(event: ErrorEvent):
        # Ловит всё, что не поймано внутри самих хендлеров — без этого
        # необработанное исключение просто тихо логируется aiogram и апдейт
        # теряется без единого следа для пользователя. Здесь хотя бы видно,
        # что именно и когда сломалось.
        log.exception(
            f"Необработанная ошибка при обработке update={event.update.update_id}: {event.exception}"
        )
        return True

    scheduler = setup_scheduler(bot)
    scheduler.start()

    # Polling — для локальной разработки и текущего прод-деплоя.
    # При переходе на webhook эту часть заменим на aiohttp.web сервер.
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
