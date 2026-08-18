import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")
# Railway (и большинство хостингов) дают DATABASE_URL вида postgresql://...,
# а asyncpg-драйверу нужен префикс postgresql+asyncpg:// — конвертируем автоматически
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Цена и длительность подписки — легко поменять без трогания кода
SUBSCRIPTION_PRICE_STARS = int(os.environ.get("SUBSCRIPTION_PRICE_STARS", "100"))
SUBSCRIPTION_DAYS = int(os.environ.get("SUBSCRIPTION_DAYS", "30"))

# За сколько дней до окончания слать напоминание
REMINDER_DAYS_BEFORE = int(os.environ.get("REMINDER_DAYS_BEFORE", "3"))

# CryptoBot (Crypto Pay API)
CRYPTO_BOT_TOKEN = os.environ["CRYPTO_BOT_TOKEN"]
# По умолчанию testnet — безопасно для разработки. На проде сменить на
# https://pay.crypt.bot/api/ и токен от @CryptoBot (не Testnet)
CRYPTO_BOT_API_URL = os.environ.get("CRYPTO_BOT_API_URL", "https://testnet-pay.crypt.bot/api/")
SUBSCRIPTION_PRICE_USDT = os.environ.get("SUBSCRIPTION_PRICE_USDT", "5")
# round(), а не int() — int() просто отбрасывает дробную часть и может
# занижать цену на копейку при "некруглых" ценах типа 4.99
SUBSCRIPTION_PRICE_USDT_CENTS = round(float(SUBSCRIPTION_PRICE_USDT) * 100)

# Реферальная программа
REFERRAL_PERCENT = int(os.environ.get("REFERRAL_PERCENT", "30"))
# Telegram ID (не username!) того, кто может смотреть отчёт по выплатам рефереров
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

# Автовывод начислений в USDT через CryptoBot (Stars так вывести нельзя —
# у Bot API нет метода перевода произвольной суммы звёзд юзеру)
MIN_WITHDRAWAL_USDT_CENTS = round(float(os.environ.get("MIN_WITHDRAWAL_USDT", "15")) * 100)
