"""Хендлеры комнат: создание, поиск, вход/выход, готовность, настройки, кик."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.keyboards.common import back_to_menu_kb
from bot.keyboards.room import (
    create_wizard_maxp_kb,
    create_wizard_minp_kb,
    create_wizard_privacy_kb,
    players_manage_kb,
    roles_setup_kb,
    room_view_kb,
    timer_adjust_kb,
)
from bot.roles import get_role
from bot.services.game_view import roles_setup_text, room_text
from bot.services.rooms import RoomSettings
from bot.states import JoinByIdStates, PasswordStates, RoomCreationStates
from bot.utils.callbacks import MenuCB, RoomCB, RoomCreateCB
from bot.utils.helpers import esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


# ------------------------------------------------------------------ создание

@router.callback_query(MenuCB.filter(F.action == "create_room"))
async def cb_create_start(
    callback: CallbackQuery, state: FSMContext, session, services, db_user, group=None
) -> None:
    """Вход в полный визард создания комнаты (шаги 1–5: имя, максимум,
    минимум, приватность, роли) — ОДИН и тот же flow для ЛС и группы.

    Разница только в скоупе: из группы комната создаётся принадлежащей ЭТОЙ
    группе (group_id в FSM-данных, изоляция ТЗ-11) и требует право START_GAME
    (как /createroom); из ЛС — глобальной (group_id=None), доступно всем.
    """
    if group is not None:
        from bot.services.permissions import Permission

        access = await services.permissions.resolve(session, db_user.telegram_id, group.id)
        if Permission.START_GAME not in access.permissions:
            await callback.answer("⛔️ Нет права START_GAME", show_alert=True)
            return
    await callback.answer()
    await state.clear()
    if group is not None:
        await state.update_data(group_id=group.id)
        title = "🏠 <b>СОЗДАНИЕ КОМНАТЫ ГРУППЫ · шаг 1 из 5</b>"
    else:
        title = "🏠 <b>СОЗДАНИЕ КОМНАТЫ · шаг 1 из 5</b>"
    await state.set_state(RoomCreationStates.name)
    await edit_or_answer(
        callback,
        f"{title}\n\n"
        "Придумай название комнаты (3–64 символа).\n"
        "В любой момент: /cancel — отмена.",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    await state.clear()
    if current is not None:
        await message.answer("❌ Отменено.", reply_markup=back_to_menu_kb())


@router.message(RoomCreationStates.name)
async def wizard_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not (3 <= len(name) <= 64):
        await message.answer("Название должно быть от 3 до 64 символов. Попробуй ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(RoomCreationStates.max_players)
    await message.answer(
        "👥 <b>Шаг 2 из 5.</b> Максимальное количество игроков:",
        reply_markup=create_wizard_maxp_kb(),
    )


@router.callback_query(RoomCreateCB.filter(F.action == "maxp"))
async def wizard_maxp(
    callback: CallbackQuery, callback_data: RoomCreateCB, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(max_players=int(callback_data.value))
    await state.set_state(RoomCreationStates.min_players)
    await edit_or_answer(
        callback,
        f"👥 <b>Шаг 3 из 5.</b> Максимум: {callback_data.value}. Минимум для старта:",
        create_wizard_minp_kb(int(callback_data.value)),
    )


@router.callback_query(RoomCreateCB.filter(F.action == "minp"))
async def wizard_minp(
    callback: CallbackQuery, callback_data: RoomCreateCB, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(min_players=int(callback_data.value))
    await state.set_state(RoomCreationStates.privacy)
    await edit_or_answer(callback, "🔒 <b>Шаг 4 из 5.</b> Тип комнаты:", create_wizard_privacy_kb())


@router.callback_query(RoomCreateCB.filter(F.action == "privacy"))
async def wizard_privacy(
    callback: CallbackQuery, callback_data: RoomCreateCB, state: FSMContext, services
) -> None:
    await callback.answer()
    is_private = callback_data.value == "private"
    await state.update_data(is_private=is_private)
    if is_private:
        await state.set_state(RoomCreationStates.password)
        await edit_or_answer(
            callback,
            "🔐 <b>Шаг 4.5.</b> Пришли пароль комнаты (4–32 символа). "
            "Передавай его только тем, кого хочешь пустить.",
        )
        return
    await state.update_data(password=None)
    await _show_roles_step(callback, state, services)


@router.message(RoomCreationStates.password)
async def wizard_password(message: Message, state: FSMContext, services) -> None:
    password = (message.text or "").strip()
    if not (4 <= len(password) <= 32):
        await message.answer("Пароль: 4–32 символа. Попробуй ещё раз.")
        return
    await state.update_data(password=password)
    await _show_roles_step(message, state, services)


async def _show_roles_step(event, state: FSMContext, services) -> None:
    """Шаг 5: интерактивная настройка состава ролей."""
    data = await state.get_data()
    defaults: RoomSettings = await services.rooms.default_settings()
    setup = dict(defaults.roles)
    await state.update_data(roles=setup)
    await state.set_state(RoomCreationStates.roles)
    app_cfg = await services.app_config.get()
    keyboard = roles_setup_kb(0, setup, app_cfg.enabled_role_objects())
    text = (
        "🎭 <b>Шаг 5 из 5. НАБОР РОЛЕЙ</b>\n\n"
        "Настраивай состав кнопками ➖/➕ (нажми на роль — описание).\n"
        "Все, кому не достанется роль, станут 🔵 мирными.\n\n"
        + roles_setup_text(setup, data.get("max_players", 10))
        + "\n\nКогда готов — нажми «✅ Создать комнату»."
    )
    keyboard = _replace_done_button(keyboard, "✅ Создать комнату")
    if isinstance(event, CallbackQuery):
        await edit_or_answer(event, text, keyboard)
    else:
        await event.answer(text, reply_markup=keyboard)


def _replace_done_button(keyboard: InlineKeyboardMarkup, title: str) -> InlineKeyboardMarkup:
    """Меняет подпись последней кнопки (Готово -> Создать комнату)."""
    rows = [list(row) for row in keyboard.inline_keyboard]
    if rows and rows[-1]:
        last = rows[-1][-1]
        rows[-1][-1] = InlineKeyboardButton(text=title, callback_data=last.callback_data)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_roles_editor(
    room_id: int, setup: dict[str, int], max_players: int, services, done_title: str = "✅ Готово"
) -> tuple[str, InlineKeyboardMarkup]:
    app_cfg = await services.app_config.get()
    keyboard = roles_setup_kb(room_id, setup, app_cfg.enabled_role_objects())
    keyboard = _replace_done_button(keyboard, done_title)
    text = (
        "🎭 <b>НАБОР РОЛЕЙ</b>\n\n"
        "➖/➕ — изменить количество, нажми на роль — описание.\n"
        "Остальные игроки станут 🔵 мирными.\n\n" + roles_setup_text(setup, max_players)
    )
    return text, keyboard


# --------------------------------------------------- роли: инкремент/декремент

async def _mutate_room_roles(
    callback: CallbackQuery, callback_data: RoomCB, services, db_user, delta: int
) -> None:
    room_id = callback_data.room_id
    if room_id == 0:
        # Ещё мастер создания — храним в FSM
        await callback.answer()
        return

    def mutate(settings: RoomSettings) -> RoomSettings:
        settings.roles[callback_data.value] = max(
            0, settings.roles.get(callback_data.value, 0) + delta
        )
        return settings

    # room.creator_id — внутренний DB users.id, а from_user.id — Telegram ID.
    # Передаём db_user.id (как close/kick/leave/start): иначе реальный
    # создатель получал отказ «Настройки меняет только создатель».
    room, message = await services.rooms.update_settings(room_id, db_user.id, mutate)
    if room is None:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer("Изменено" if "сохранены" in message else message[:100])
    await _render_room_by_id(callback, services, room.id, db_user.id)


@router.callback_query(RoomCB.filter(F.action == "roleinc"))
async def cb_role_inc(
    callback: CallbackQuery, callback_data: RoomCB, services, state: FSMContext, db_user
) -> None:
    if callback_data.room_id == 0:
        await _wizard_role_change(callback, callback_data, state, services, +1)
        return
    await _mutate_room_roles(callback, callback_data, services, db_user, +1)


@router.callback_query(RoomCB.filter(F.action == "roledec"))
async def cb_role_dec(
    callback: CallbackQuery, callback_data: RoomCB, services, state: FSMContext, db_user
) -> None:
    if callback_data.room_id == 0:
        await _wizard_role_change(callback, callback_data, state, services, -1)
        return
    await _mutate_room_roles(callback, callback_data, services, db_user, -1)


async def _wizard_role_change(
    callback: CallbackQuery, callback_data: RoomCB, state: FSMContext, services, delta: int
) -> None:
    data = await state.get_data()
    setup: dict = dict(data.get("roles", {}))
    setup[callback_data.value] = max(0, setup.get(callback_data.value, 0) + delta)
    await state.update_data(roles=setup)
    await callback.answer()
    text, keyboard = await _render_roles_editor(
        0, setup, data.get("max_players", 10), services, done_title="✅ Создать комнату"
    )
    await edit_or_answer(callback, text, keyboard)


@router.callback_query(RoomCB.filter(F.action == "roleinfo"))
async def cb_role_info(callback: CallbackQuery, callback_data: RoomCB) -> None:
    role = get_role(callback_data.value)
    if role is None:
        await callback.answer("Роль не найдена", show_alert=True)
        return
    await callback.answer(
        f"{role.title}: {role.description[:180]}", show_alert=True
    )


@router.callback_query(RoomCB.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# --------------------------------------------------- мастер: создание комнаты

@router.callback_query(RoomCB.filter(F.action == "settings_done"))
async def cb_settings_done(callback: CallbackQuery, callback_data: RoomCB, state: FSMContext, services, db_user) -> None:
    if callback_data.room_id == 0:
        data = await state.get_data()
        # визард, запущенный в группе, создаёт комнату ЭТОЙ группы (ТЗ-11);
        # запущенный в ЛС — глобальную (group_id=None)
        room, message = await services.rooms.create_room(
            creator_user_id=db_user.id,
            name=data.get("name", "Комната"),
            max_players=int(data.get("max_players", 10)),
            min_players=int(data.get("min_players", 4)),
            is_private=bool(data.get("is_private", False)),
            password=data.get("password"),
            roles=dict(data.get("roles", {})),
            group_id=data.get("group_id"),
        )
        await state.clear()
        if room is None:
            await callback.answer(message, show_alert=True)
            return
        await callback.answer("Комната создана!")
        await _render_room_by_id(callback, services, room.id, db_user.id)
        return

    async with services.session_factory() as session:
        room = await RoomRepository(session).get(callback_data.room_id)
    if room is None:
        await callback.answer("Комната не найдена", show_alert=True)
        return
    await callback.answer()
    await _render_room_by_id(callback, services, room.id, db_user.id)


# ------------------------------------------------------------------- просмотр

async def _render_room_by_id(callback: CallbackQuery, services, room_id: int, user_id: int) -> None:
    """Перерисовка комнаты по ID из свежей сессии."""
    async with services.session_factory() as session:
        room = await RoomRepository(session).get(room_id)
    if room is None:
        await edit_or_answer(callback, "Комната не найдена.", back_to_menu_kb())
        return
    game = None
    if room.game_id:
        game = await GameRepository(session).get(room.game_id)
    await edit_or_answer(callback, room_text(room, room.players, game), room_view_kb(room, user_id))


@router.callback_query(RoomCB.filter(F.action == "view"))
async def cb_room_view(callback: CallbackQuery, callback_data: RoomCB, session, db_user) -> None:
    await callback.answer()
    room = await RoomRepository(session).get(callback_data.room_id)
    if room is None:
        await edit_or_answer(callback, "Комната не найдена.", back_to_menu_kb())
        return
    game = None
    if room.game_id:
        game = await GameRepository(session).get(room.game_id)
    await edit_or_answer(callback, room_text(room, room.players, game), room_view_kb(room, db_user.id))


@router.callback_query(RoomCB.filter(F.action == "tomenu"))
async def cb_to_menu(callback: CallbackQuery) -> None:
    from bot.keyboards.common import main_menu_kb

    await callback.answer()
    await edit_or_answer(callback, "🎭 <b>Главное меню</b>\n\nВыбери действие 👇", main_menu_kb())


@router.callback_query(RoomCB.filter(F.action == "find_refresh"))
async def cb_find_refresh(callback: CallbackQuery, session, db_user, group=None) -> None:
    from bot.handlers.start import cb_play

    await cb_play(callback, session, db_user, group=group)


# -------------------------------------------------------------------- вход

@router.callback_query(RoomCB.filter(F.action == "join"))
async def cb_room_join(
    callback: CallbackQuery, callback_data: RoomCB, state: FSMContext, services, db_user
) -> None:
    rooms = services.rooms
    # Проверяем, существует ли комната (для пароля нужно её знать)
    async with services.session_factory() as session:
        room = await RoomRepository(session).get(callback_data.room_id)
    if room is None:
        await callback.answer("Комната не найдена", show_alert=True)
        return
    if room.is_private:
        await state.set_state(PasswordStates.password)
        await state.update_data(join_room_id=room.id)
        await callback.answer("🔐 Комната приватная", show_alert=False)
        await edit_or_answer(callback, f"🔐 Комната #{room.id} приватная.\nПришли пароль (или /cancel).")
        return

    active_game = await _active_game_gp(services, db_user.id)
    if active_game:
        await callback.answer("Ты уже в активной игре!", show_alert=True)
        return

    joined_room, message = await rooms.join(room.id, db_user.id)
    if joined_room is None:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer(message)
    await _render_room_by_id(callback, services, room.id, db_user.id)


async def _active_game_gp(services, user_id: int):
    async with services.session_factory() as session:
        return await GamePlayerRepository(session).active_game_of_user(user_id)


@router.message(PasswordStates.password)
async def process_password(message: Message, state: FSMContext, services, db_user) -> None:
    data = await state.get_data()
    room_id = int(data.get("join_room_id", 0))
    await state.clear()
    room, result = await services.rooms.join(room_id, db_user.id, password=(message.text or "").strip())
    if room is None:
        await message.answer(f"❌ {esc(result)}")
        return
    await message.answer("✅ " + esc(result))
    async with services.session_factory() as session:
        fresh = await RoomRepository(session).get(room_id)
    from bot.keyboards.room import room_view_kb as _kb

    if fresh:
        await message.answer(room_text(fresh, fresh.players), reply_markup=_kb(fresh, db_user.id))


@router.callback_query(RoomCB.filter(F.action == "join_by_id"))
async def cb_join_by_id(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(JoinByIdStates.room_id)
    await edit_or_answer(callback, "➕ Пришли ID комнаты (число из заголовка, например 4821).")


@router.message(JoinByIdStates.room_id)
async def process_join_by_id(message: Message, state: FSMContext, services, db_user) -> None:
    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Нужен числовой ID комнаты. Попробуй ещё раз или /cancel.")
        return
    room_id = int(raw)
    await state.clear()
    async with services.session_factory() as session:
        room = await RoomRepository(session).get(room_id)
    if room is None:
        await message.answer("Комната не найдена.")
        return
    if room.is_private:
        await state.set_state(PasswordStates.password)
        await state.update_data(join_room_id=room.id)
        await message.answer("🔐 Комната приватная. Пришли пароль.")
        return
    joined, result = await services.rooms.join(room_id, db_user.id)
    if joined is None:
        await message.answer(f"❌ {esc(result)}")
        return
    await message.answer("✅ " + esc(result))
    async with services.session_factory() as session:
        fresh = await RoomRepository(session).get(room_id)
    if fresh:
        await message.answer(
            room_text(fresh, fresh.players), reply_markup=room_view_kb(fresh, db_user.id)
        )


# -------------------------------------------------------------------- выход

@router.callback_query(RoomCB.filter(F.action == "leave"))
async def cb_room_leave(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    room, message = await services.rooms.leave(callback_data.room_id, db_user.id)
    if room is None:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer(message)
    await edit_or_answer(callback, "Ты покинул комнату.", back_to_menu_kb())


@router.callback_query(RoomCB.filter(F.action == "ready"))
async def cb_room_ready(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    ready = callback_data.value == "1"
    room, message = await services.rooms.set_ready(callback_data.room_id, db_user.id, ready)
    if room is None:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer(message)
    async with services.session_factory() as session:
        fresh = await RoomRepository(session).get(room.id)
    if fresh:
        await edit_or_answer(callback, room_text(fresh, fresh.players), room_view_kb(fresh, db_user.id))


# --------------------------------------------------------------- создатель

@router.callback_query(RoomCB.filter(F.action == "players"))
async def cb_room_players(callback: CallbackQuery, callback_data: RoomCB, session, db_user) -> None:
    await callback.answer()
    room = await RoomRepository(session).get(callback_data.room_id)
    if room is None or room.creator_id != db_user.id:
        await callback.answer("Доступно только создателю", show_alert=True)
        return
    await edit_or_answer(
        callback,
        "👥 <b>ИГРОКИ КОМНАТЫ</b>\n\nНажми на игрока, чтобы исключить:",
        players_manage_kb(room, db_user.id),
    )


@router.callback_query(RoomCB.filter(F.action == "kick"))
async def cb_room_kick(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    target_id = int(callback_data.value)
    room, message = await services.rooms.kick(callback_data.room_id, db_user.id, target_id)
    if room is None:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer(message)
    async with services.session_factory() as session:
        fresh = await RoomRepository(session).get(room.id)
    if fresh:
        await edit_or_answer(
            callback,
            "👥 <b>ИГРОКИ КОМНАТЫ</b>\n\nНажми на игрока, чтобы исключить:",
            players_manage_kb(fresh, db_user.id),
        )


@router.callback_query(RoomCB.filter(F.action == "close"))
async def cb_room_close(callback: CallbackQuery, callback_data: RoomCB, services) -> None:
    from bot.keyboards.room import confirm_kb

    await callback.answer()
    await edit_or_answer(
        callback,
        "❌ Закрыть комнату? Все игроки будут исключены.",
        confirm_kb(
            RoomCB(action="close_yes", room_id=callback_data.room_id).pack(),
            RoomCB(action="view", room_id=callback_data.room_id).pack(),
        ),
    )


@router.callback_query(RoomCB.filter(F.action == "close_yes"))
async def cb_room_close_yes(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    message = await services.rooms.close_room(callback_data.room_id, db_user.id)
    await callback.answer(message[:100])
    await edit_or_answer(callback, esc(message), back_to_menu_kb())


@router.callback_query(RoomCB.filter(F.action == "start"))
async def cb_room_start(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    result = await services.games.start_game_from_room(callback_data.room_id, db_user.id)
    await callback.answer(result.message[:180], show_alert=not result.ok)
    if result.ok:
        await edit_or_answer(callback, esc(result.message), back_to_menu_kb())


@router.callback_query(RoomCB.filter(F.action == "game"))
async def cb_room_game(callback: CallbackQuery, callback_data: RoomCB, session, db_user) -> None:
    from bot.utils.callbacks import GameCB

    await callback.answer()
    room = await RoomRepository(session).get(callback_data.room_id)
    if room is None or not room.game_id:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    await edit_or_answer(
        callback,
        "🎮 Игра идёт. Открой состояние:",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🎮 Состояние игры",
                callback_data=GameCB(action="status", game_id=room.game_id).pack(),
            )
        ]]),
    )


# ------------------------------------------------------------------ настройки

@router.callback_query(RoomCB.filter(F.action == "settings"))
async def cb_room_settings(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    await callback.answer()
    async with services.session_factory() as session:
        room = await RoomRepository(session).get(callback_data.room_id)
    if room is None or room.creator_id != db_user.id:
        await callback.answer("Настройки доступны создателю", show_alert=True)
        return
    settings = RoomSettings.from_room(room)
    text, keyboard = await _render_roles_editor(room.id, settings.roles, room.max_players, services)
    await edit_or_answer(callback, text, keyboard)


@router.callback_query(RoomCB.filter(F.action == "timers"))
async def cb_room_timers(callback: CallbackQuery, callback_data: RoomCB) -> None:
    await callback.answer()
    await edit_or_answer(
        callback,
        "⏱ <b>ТАЙМЕРЫ ФАЗ</b>\n\nНастраиваются кнопками ±30 секунд.",
        timer_adjust_kb(callback_data.room_id),
    )


@router.callback_query(RoomCB.filter(F.action == "timer"))
async def cb_room_timer_set(
    callback: CallbackQuery, callback_data: RoomCB, services, db_user
) -> None:
    # значение вида "night.+30": "." — не разделитель CallbackData
    # (двоеточие в value невозможно — pack() отклоняет его)
    phase, _, raw_delta = callback_data.value.partition(".")
    delta = int(raw_delta)
    key = f"{phase}_seconds"

    def mutate(settings: RoomSettings) -> RoomSettings:
        current = getattr(settings, key)
        setattr(settings, key, max(30, min(600, current + delta)))
        return settings

    room, message = await services.rooms.update_settings(callback_data.room_id, db_user.id, mutate)
    if room is None:
        await callback.answer(message, show_alert=True)
        return
    settings = RoomSettings.from_room(room)
    await callback.answer(
        f"🌙 {settings.night_seconds}с · ☀️ {settings.day_seconds}с · 🗳 {settings.vote_seconds}с"
    )
    await edit_or_answer(
        callback,
        "⏱ <b>ТАЙМЕРЫ ФАЗ</b>\n\n"
        f"🌙 Ночь: {settings.night_seconds} сек\n"
        f"☀️ День: {settings.day_seconds} сек\n"
        f"🗳 Голосование: {settings.vote_seconds} сек",
        timer_adjust_kb(room.id),
    )


@router.callback_query(RoomCB.filter(F.action == "tie"))
async def cb_room_tie(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    def mutate(settings: RoomSettings) -> RoomSettings:
        settings.tie_rule = "no_death" if settings.tie_rule == "revote" else "revote"
        return settings

    room, _ = await services.rooms.update_settings(callback_data.room_id, db_user.id, mutate)
    if room is None:
        await callback.answer("Недоступно", show_alert=True)
        return
    settings = RoomSettings.from_room(room)
    rule = "⚖️ Ничья → повторное голосование" if settings.tie_rule == "revote" else "🌿 Ничья → никто не умирает"
    await callback.answer(rule[:100])
    await edit_or_answer(callback, f"⚖️ <b>ПРАВИЛО НИЧЬЕЙ</b>\n\nТекущее: {rule}", timer_adjust_kb(room.id))


@router.callback_query(RoomCB.filter(F.action == "reveal"))
async def cb_room_reveal(callback: CallbackQuery, callback_data: RoomCB, services, db_user) -> None:
    def mutate(settings: RoomSettings) -> RoomSettings:
        settings.reveal_roles_on_death = not settings.reveal_roles_on_death
        return settings

    room, _ = await services.rooms.update_settings(callback_data.room_id, db_user.id, mutate)
    if room is None:
        await callback.answer("Недоступно", show_alert=True)
        return
    settings = RoomSettings.from_room(room)
    state = "раскрываются" if settings.reveal_roles_on_death else "скрываются"
    await callback.answer(f"Роли при смерти: {state}")
    await edit_or_answer(
        callback,
        f"💀 <b>РАСКРЫТИЕ РОЛЕЙ ПРИ СМЕРТИ</b>\n\nСейчас роли погибших {state}.",
        timer_adjust_kb(room.id),
    )
