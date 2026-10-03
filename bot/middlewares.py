import time
import logging

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery

log = logging.getLogger(__name__)

# Минимальный интервал между действиями ОДНОГО юзера, в секундах.
# Не научная величина — достаточно мала, чтобы не мешать обычному
# пользованию (клик раз в секунду это нормально), но отсекает спам кликами.
THROTTLE_INTERVAL = 0.7

# Модульный (не инстанс-level) словарь — так лимит общий для message И
# callback_query одновременно: нельзя обойти троттлинг, чередуя команды и кнопки.
_last_action: dict[int, float] = {}


class ThrottlingMiddleware(BaseMiddleware):
    """Не даёт одному юзеру засыпать бота действиями чаще, чем раз в
    THROTTLE_INTERVAL секунд. Защищает от:
    - лишней нагрузки на БД при бессмысленном спаме кнопками меню
    - повторных вызовов внешних API (CryptoBot) при частом клике на оплату
    - риска словить флуд-контроль самого Telegram при массовых апдейтах

    Регистрируется как outer_middleware — блокирует ДО того, как апдейт
    вообще дойдёт до хендлера, то есть лишней работы не происходит совсем."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        last = _last_action.get(user.id, 0.0)

        if now - last < THROTTLE_INTERVAL:
            if isinstance(event, CallbackQuery):
                try:
                    # Просто снимаем "часики" с кнопки, без текста — не нужно
                    # дополнительно спамить алертами на каждый лишний клик
                    await event.answer()
                except Exception:
                    pass
            return None  # хендлер не вызывается вообще — апдейт тихо отброшен

        _last_action[user.id] = now
        return await handler(event, data)
