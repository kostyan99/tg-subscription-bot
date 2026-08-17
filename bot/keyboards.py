from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import SUBSCRIPTION_PRICE_STARS, SUBSCRIPTION_PRICE_USDT


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка", callback_data="menu_subscription")],
            [InlineKeyboardButton(text="🔗 Реферальная программа", callback_data="menu_referral")],
        ]
    )


def subscription_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⭐ Оплатить {SUBSCRIPTION_PRICE_STARS} Stars",
                    callback_data="subscribe_stars",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"₿ Оплатить {SUBSCRIPTION_PRICE_USDT} USDT (крипта)",
                    callback_data="subscribe_crypto",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
        ]
    )


def referral_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Кого пригласил", callback_data="referral_list_0")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
        ]
    )


def referral_list_kb(page: int, has_next: bool) -> InlineKeyboardMarkup:
    nav_row = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"referral_list_{page - 1}")
        )
    if has_next:
        nav_row.append(
            InlineKeyboardButton(text="След. ➡️", callback_data=f"referral_list_{page + 1}")
        )

    keyboard = [nav_row] if nav_row else []
    keyboard.append([InlineKeyboardButton(text="⬅️ К реферальной программе", callback_data="menu_referral")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def back_kb(target: str = "menu_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]]
    )
