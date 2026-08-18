import logging

import aiohttp

from bot.config import CRYPTO_BOT_TOKEN, CRYPTO_BOT_API_URL

log = logging.getLogger(__name__)

# Без таймаута зависший запрос к CryptoBot может заблокировать scheduler навсегда
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CryptoBotError(Exception):
    pass


async def create_invoice(amount: str, asset: str, payload: str, description: str) -> dict:
    """Создаёт инвойс в CryptoBot. Возвращает dict с полями invoice_id, pay_url, status и т.д."""
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(
            CRYPTO_BOT_API_URL + "createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={
                "amount": amount,
                "asset": asset,
                "payload": payload,
                "description": description,
                "expires_in": 3600,  # инвойс живёт час
            },
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.error(f"CryptoBot createInvoice error: {data}")
                raise CryptoBotError(str(data))
            return data["result"]


async def get_invoices_statuses(invoice_ids: list[int]) -> dict[int, str]:
    """Батч-проверка статусов сразу нескольких инвойсов ОДНИМ запросом —
    вместо N запросов на N pending-заказов. CryptoBot принимает список ID
    через запятую в одном вызове getInvoices."""
    if not invoice_ids:
        return {}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.get(
            CRYPTO_BOT_API_URL + "getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": ",".join(str(i) for i in invoice_ids)},
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.error(f"CryptoBot getInvoices error: {data}")
                return {}
            return {item["invoice_id"]: item["status"] for item in data["result"]["items"]}


async def transfer(user_id: int, asset: str, amount: str, spend_id: str, comment: str = "") -> dict:
    """Отправляет крипту с баланса приложения на аккаунт юзера в CryptoBot.

    ВАЖНО: юзер должен был хотя бы раз написать /start CryptoBot'у, иначе
    перевод не пройдёт. И метод transfer нужно явно включить в настройках
    приложения: @CryptoBot -> Crypto Pay -> My Apps -> твоё приложение ->
    Security -> Transfers -> Enable. Без этого шага запрос будет падать
    с ошибкой авторизации метода, даже если сам API-токен верный.

    spend_id — обязателен для идемпотентности: повторный вызов с тем же
    spend_id не создаст второй перевод, а просто вернёт исходный результат.
    Из-за этого именно spend_id, а не отдельная проверка "уже выводили?" —
    настоящая защита от двойного списания при повторных кликах/ретраях.
    """
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async with session.post(
            CRYPTO_BOT_API_URL + "transfer",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={
                "user_id": user_id,
                "asset": asset,
                "amount": amount,
                "spend_id": spend_id,
                "comment": comment,
            },
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.error(f"CryptoBot transfer error: {data}")
                raise CryptoBotError(str(data))
            return data["result"]
