"""Клавиатуры админ-панели."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.roles import all_roles
from bot.utils.callbacks import AdminCB


def admin_panel_kb(debug_mode: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data=AdminCB(action="stats").pack())],
        [
            InlineKeyboardButton(text="🎮 Активные игры", callback_data=AdminCB(action="games").pack()),
            InlineKeyboardButton(text="🏠 Комнаты", callback_data=AdminCB(action="rooms").pack()),
        ],
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data=AdminCB(action="ban").pack()),
            InlineKeyboardButton(text="✅ Разбан", callback_data=AdminCB(action="unban").pack()),
        ],
        [
            InlineKeyboardButton(text="📣 Рассылка", callback_data=AdminCB(action="broadcast").pack()),
            InlineKeyboardButton(text="📜 Логи", callback_data=AdminCB(action="logs").pack()),
        ],
        [
            InlineKeyboardButton(text="🎭 Роли", callback_data=AdminCB(action="roles").pack()),
            InlineKeyboardButton(text="⚙️ Параметры", callback_data=AdminCB(action="gparams").pack()),
        ],
    ]
    if debug_mode:
        # DEBUG MODE: тестовые игры с ботами (/testgame)
        rows.append([
            InlineKeyboardButton(
                text="🧪 Тестовая игра", callback_data=AdminCB(action="testgame").pack()
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="panel").pack())]
    ])


def admin_roles_kb(enabled: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for role in all_roles():
        if role.id == "citizen":
            continue
        mark = "✅" if role.id in enabled else "⛔"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {role.emoji} {role.name}",
                callback_data=AdminCB(action="roletoggle", value=role.id).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="panel").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_games_kb(games: list) -> InlineKeyboardMarkup:
    rows = []
    for game in games:
        rows.append([
            InlineKeyboardButton(
                text=f"🏁 Завершить #{game.id} ({game.status}, день {game.day_number})",
                callback_data=AdminCB(action="endgame", value=str(game.id)).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="panel").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_rooms_kb(rooms: list) -> InlineKeyboardMarkup:
    rows = []
    for room in rooms:
        rows.append([
            InlineKeyboardButton(
                text=f"❌ Закрыть #{room.id} «{room.name[:16]}» [{room.status}]",
                callback_data=AdminCB(action="closeroom", value=str(room.id)).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="panel").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_confirm_end_kb(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Завершить", callback_data=AdminCB(action="endgame_yes", value=str(game_id)).pack()
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data=AdminCB(action="games").pack()),
    ]])


def admin_params_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🌙 Ночь", callback_data=AdminCB(action="setparam", value="night_seconds").pack()),
            InlineKeyboardButton(text="☀️ День", callback_data=AdminCB(action="setparam", value="day_seconds").pack()),
            InlineKeyboardButton(text="🗳 Голос.", callback_data=AdminCB(action="setparam", value="vote_seconds").pack()),
        ],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data=AdminCB(action="panel").pack())],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
