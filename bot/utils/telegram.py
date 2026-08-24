"""Помощники для работы с Telegram API."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def edit_or_answer(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    """Редактирует сообщение колбэка; если нельзя — присылает новое."""
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return  # двойное нажатие — просто игнорируем
            await message.answer(text, reply_markup=keyboard)
