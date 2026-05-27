from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)


def get_main_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🍎 Дневник сегодня", callback_data="diary_today")],
        [InlineKeyboardButton(text="📅 Статистика за неделю", callback_data="diary_week")],
        [InlineKeyboardButton(text="💡 Можно ли мне это?", callback_data="ask_food")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📊 Статистика за день"),
                KeyboardButton(text="📅 Неделя"),
            ],
            [
                KeyboardButton(text="💪 Добавить тренировку"),
                KeyboardButton(text="🧹 Сбросить день"),
            ],
        ],
        resize_keyboard=True,
    )
