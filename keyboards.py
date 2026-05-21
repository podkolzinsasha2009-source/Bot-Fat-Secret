from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🍎 Дневник сегодня", callback_data="diary_today")],
        [InlineKeyboardButton(text="📅 Календарь (неделя)", callback_data="diary_week")],
        [InlineKeyboardButton(text="💡 Можно ли мне это?", callback_data="ask_food")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
