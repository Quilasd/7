"""Клавиатуры. Все — inline, собираются из CallbackData-фабрик."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def back_button(callback_data: str, text: str = "⬅️ Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def main_menu_kb() -> InlineKeyboardMarkup:
    from bot.utils.callbacks import MenuCB

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Играть", callback_data=MenuCB(action="play").pack())],
        [
            InlineKeyboardButton(text="🏠 Создать комнату", callback_data=MenuCB(action="create_room").pack()),
            InlineKeyboardButton(text="🔎 Найти игру", callback_data=MenuCB(action="find").pack()),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data=MenuCB(action="profile").pack()),
            InlineKeyboardButton(text="🏆 Рейтинг", callback_data=MenuCB(action="rating").pack()),
        ],
        [
            InlineKeyboardButton(text="📖 Правила", callback_data=MenuCB(action="rules").pack()),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=MenuCB(action="settings").pack()),
        ],
    ])


def back_to_menu_kb() -> InlineKeyboardMarkup:
    from bot.utils.callbacks import MenuCB

    return InlineKeyboardMarkup(inline_keyboard=[
        [back_button(MenuCB(action="main").pack())],
    ])
