"""Клавиатуры тестового режима (DEBUG MODE)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.callbacks import TestCB


def test_players_count_kb() -> InlineKeyboardMarkup:
    """Выбор количества участников тестовой игры."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"🤖 {count} игроков",
                callback_data=TestCB(action="create", value=str(count)).pack(),
            )
        ]
        for count in (4, 5, 6)
    ]
    rows.append([
        InlineKeyboardButton(text="⬅️ В админку", callback_data=TestCB(action="toadmin").pack())
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def test_controls_kb(game_id: int, auto_on: bool) -> InlineKeyboardMarkup:
    """Пульт управления тестовой игрой."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📋 Состояние", callback_data=TestCB(action="status", value=str(game_id)).pack()
            ),
            InlineKeyboardButton(
                text="⏩ Пропустить фазу", callback_data=TestCB(action="skip", value=str(game_id)).pack()
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤖 Действия ботов", callback_data=TestCB(action="actnow", value=str(game_id)).pack()
            ),
            InlineKeyboardButton(
                text=("🟢 Авто ботов: ВКЛ" if auto_on else "🔴 Авто ботов: ВЫКЛ"),
                callback_data=TestCB(action="auto", value=str(game_id)).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="🏁 Завершить игру", callback_data=TestCB(action="finish", value=str(game_id)).pack()
            ),
        ],
    ])
