"""Клавиатуры комнаты: просмотр, роли, настройки, поиск."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database.models import Room, RoomStatus
from bot.roles import Role
from bot.utils.callbacks import RoomCB, RoomCreateCB
from bot.utils.helpers import display_name


def room_view_kb(room: Room, user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура комнаты: обычный игрок / создатель."""
    is_creator = room.creator_id == user_id
    membership = next((m for m in room.players if m.user_id == user_id), None)
    rows: list[list[InlineKeyboardButton]] = []

    if room.status == RoomStatus.OPEN.value:
        if membership:
            ready = membership.is_ready
            rows.append([
                InlineKeyboardButton(
                    text="🟡 Не готов" if ready else "🟢 Готов",
                    callback_data=RoomCB(action="ready", room_id=room.id, value=str(int(not ready))).pack(),
                ),
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=RoomCB(action="view", room_id=room.id).pack(),
                ),
            ])
            if not is_creator:
                rows.append([
                    InlineKeyboardButton(
                        text="🚪 Выйти",
                        callback_data=RoomCB(action="leave", room_id=room.id).pack(),
                    )
                ])
        else:
            rows.append([
                InlineKeyboardButton(
                    text="➕ Присоединиться",
                    callback_data=RoomCB(action="join", room_id=room.id).pack(),
                ),
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=RoomCB(action="view", room_id=room.id).pack(),
                ),
            ])
        if is_creator:
            rows.append([
                InlineKeyboardButton(
                    text="⚙️ Настройки комнаты",
                    callback_data=RoomCB(action="settings", room_id=room.id).pack(),
                ),
                InlineKeyboardButton(
                    text="👥 Игроки/Кик",
                    callback_data=RoomCB(action="players", room_id=room.id).pack(),
                ),
            ])
            rows.append([
                InlineKeyboardButton(
                    text="▶️ Начать игру",
                    callback_data=RoomCB(action="start", room_id=room.id).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Закрыть комнату",
                    callback_data=RoomCB(action="close", room_id=room.id).pack(),
                ),
            ])
    elif room.status == RoomStatus.PLAYING.value and room.game_id:
        rows.append([
            InlineKeyboardButton(
                text="🎮 Состояние игры",
                callback_data=RoomCB(action="game", room_id=room.id).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=RoomCB(action="tomenu", room_id=room.id).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def roles_setup_kb(room_id: int, setup: dict[str, int], enabled_roles: list[Role]) -> InlineKeyboardMarkup:
    """Плюс/минус по каждой роли + таймеры + правила."""
    rows: list[list[InlineKeyboardButton]] = []
    for role in enabled_roles:
        count = setup.get(role.id, 0)
        rows.append([
            InlineKeyboardButton(
                text=f"{role.emoji} {role.name}",
                callback_data=RoomCB(action="roleinfo", room_id=room_id, value=role.id).pack(),
            ),
            InlineKeyboardButton(
                text="➖", callback_data=RoomCB(action="roledec", room_id=room_id, value=role.id).pack()
            ),
            InlineKeyboardButton(
                text=str(count),
                callback_data=RoomCB(action="noop", room_id=room_id, value=role.id).pack(),
            ),
            InlineKeyboardButton(
                text="➕", callback_data=RoomCB(action="roleinc", room_id=room_id, value=role.id).pack()
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="⏱ Таймеры фаз", callback_data=RoomCB(action="timers", room_id=room_id).pack()),
    ])
    rows.append([
        InlineKeyboardButton(text="⚖️ Правило ничьей", callback_data=RoomCB(action="tie", room_id=room_id).pack()),
        InlineKeyboardButton(text="💀 Раскрытие ролей", callback_data=RoomCB(action="reveal", room_id=room_id).pack()),
    ])
    rows.append([
        InlineKeyboardButton(text="✅ Готово", callback_data=RoomCB(action="settings_done", room_id=room_id).pack()),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timer_adjust_kb(room_id: int) -> InlineKeyboardMarkup:
    rows = []
    for phase_key, title in (("night", "🌙 Ночь"), ("day", "☀️ День"), ("vote", "🗳 Голосование")):
        rows.append([
            InlineKeyboardButton(text=title, callback_data=RoomCB(action="noop", room_id=room_id, value=phase_key).pack()),
            InlineKeyboardButton(
                text="−30с", callback_data=RoomCB(action="timer", room_id=room_id, value=f"{phase_key}.-30").pack()
            ),
            InlineKeyboardButton(
                text="+30с", callback_data=RoomCB(action="timer", room_id=room_id, value=f"{phase_key}.+30").pack()
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ К настройкам", callback_data=RoomCB(action="settings", room_id=room_id).pack())
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def players_manage_kb(room: Room, creator_user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for membership in room.players:  # type: RoomPlayer
        if membership.user_id == creator_user_id:
            continue
        rows.append([
            InlineKeyboardButton(
                text=f"🚫 {display_name(membership.user)}",
                callback_data=RoomCB(action="kick", room_id=room.id, value=str(membership.user_id)).pack(),
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=RoomCB(action="view", room_id=room.id).pack())
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rooms_list_kb(rooms: list[Room], user_room: Room | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if user_room is not None:
        rows.append([
            InlineKeyboardButton(
                text=f"🎭 Моя комната #{user_room.id}",
                callback_data=RoomCB(action="view", room_id=user_room.id).pack(),
            )
        ])
    for room in rooms:
        free = room.max_players - room.player_count()
        rows.append([
            InlineKeyboardButton(
                text=f"#{room.id} · {room.name[:20]} · 👥 {room.player_count()}/{room.max_players} · свободно {free}",
                callback_data=RoomCB(action="view", room_id=room.id).pack(),
            )
        ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=RoomCB(action="find_refresh", room_id=0).pack()),
        InlineKeyboardButton(text="➕ По ID", callback_data=RoomCB(action="join_by_id", room_id=0).pack()),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data=RoomCB(action="tomenu", room_id=0).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def play_empty_kb(in_group: bool) -> InlineKeyboardMarkup:
    """Пустой экран «Играть»: действия вместо тупика.

    «🏠 Создать комнату» ведёт в тот же полный визард, что и кнопка главного
    меню (MenuCB create_room): в группе он создаёт комнату этой группы,
    в ЛС — глобальную. «➕ По ID» — вход в существующую комнату по номеру.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if in_group:
        from bot.utils.callbacks import MenuCB

        rows.append([
            InlineKeyboardButton(
                text="🏠 Создать комнату",
                callback_data=MenuCB(action="create_room").pack(),
            )
        ])
    rows.append([
        InlineKeyboardButton(text="➕ По ID", callback_data=RoomCB(action="join_by_id", room_id=0).pack()),
        InlineKeyboardButton(text="⬅️ В меню", callback_data=RoomCB(action="tomenu", room_id=0).pack()),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(yes_cb: str, no_cb: str, yes_text: str = "✅ Подтвердить", no_text: str = "❌ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=yes_text, callback_data=yes_cb),
        InlineKeyboardButton(text=no_text, callback_data=no_cb),
    ]])


def create_wizard_maxp_kb() -> InlineKeyboardMarkup:
    rows = []
    for value in (6, 8, 10, 12, 16, 20):
        rows.append([InlineKeyboardButton(text=f"👥 {value}", callback_data=RoomCreateCB(action="maxp", value=str(value)).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_wizard_minp_kb(max_players: int) -> InlineKeyboardMarkup:
    rows = []
    for value in range(4, min(max_players, 12) + 1):
        rows.append([InlineKeyboardButton(text=f"👥 {value}", callback_data=RoomCreateCB(action="minp", value=str(value)).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_wizard_privacy_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 Публичная", callback_data=RoomCreateCB(action="privacy", value="public").pack())],
        [InlineKeyboardButton(text="🔐 Приватная (с паролем)", callback_data=RoomCreateCB(action="privacy", value="private").pack())],
    ])
