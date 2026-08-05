import logging

import aiohttp

from bot.config import CRYPTO_BOT_TOKEN, CRYPTO_BOT_API_URL

log = logging.getLogger(__name__)


class CryptoBotError(Exception):
    pass


async def create_invoice(amount: str, asset: str, payload: str, description: str) -> dict:
    """Создаёт инвойс в CryptoBot. Возвращает dict с полями invoice_id, pay_url, status и т.д."""
    async with aiohttp.ClientSession() as session:
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


async def get_invoice_status(invoice_id: int) -> str | None:
    """Возвращает статус инвойса: 'active' / 'paid' / 'expired', либо None если не найден."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            CRYPTO_BOT_API_URL + "getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": str(invoice_id)},
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                log.error(f"CryptoBot getInvoices error: {data}")
                return None
            items = data["result"]["items"]
            return items[0]["status"] if items else None
