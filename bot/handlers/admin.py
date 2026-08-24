"""Админ-панель: статистика, игры, комнаты, баны, рассылка, логи, роли, параметры.

Доступ только для ADMIN_IDS из .env (проверка на каждом действии).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.database.repositories.games import GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.keyboards.admin import (
    admin_back_kb,
    admin_confirm_end_kb,
    admin_games_kb,
    admin_panel_kb,
    admin_params_kb,
    admin_roles_kb,
    admin_rooms_kb,
)
from bot.services.app_config import GlobalSettings
from bot.states import AdminStates
from bot.utils.callbacks import AdminCB
from bot.utils.helpers import esc
from bot.utils.logging import read_log_tail
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


def _is_admin(user_id: int) -> bool:
    return get_settings().is_admin(user_id)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:", reply_markup=admin_panel_kb())


async def _guard(callback: CallbackQuery) -> bool:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Недостаточно прав", show_alert=True)
        return False
    return True


@router.callback_query(AdminCB.filter(F.action == "panel"))
async def cb_admin_panel(callback: CallbackQuery) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await edit_or_answer(callback, "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:", admin_panel_kb())


@router.callback_query(AdminCB.filter(F.action == "stats"))
async def cb_admin_stats(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    users = await UserRepository(session).count_all()
    games_repo = GameRepository(session)
    active = await games_repo.count_active()
    finished = await games_repo.count_finished()
    day_games = await games_repo.count_active_today()
    rooms_open = await RoomRepository(session).count_open()
    text = "\n".join([
        "📊 <b>СТАТИСТИКА БОТА</b>",
        "",
        f"👤 Пользователей: <b>{users}</b>",
        f"🎮 Активных игр: <b>{active}</b>",
        f"🏁 Завершённых игр: <b>{finished}</b>",
        f"🕒 Игр за 24 часа: <b>{day_games}</b>",
        f"🏠 Открытых комнат: <b>{rooms_open}</b>",
    ])
    await edit_or_answer(callback, text, admin_back_kb())


@router.callback_query(AdminCB.filter(F.action == "games"))
async def cb_admin_games(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    games = await GameRepository(session).active_games()
    if not games:
        await edit_or_answer(callback, "🎮 Активных игр нет.", admin_back_kb())
        return
    lines = ["🎮 <b>АКТИВНЫЕ ИГРЫ</b>", ""]
    for game in games:
        lines.append(f"• #{game.id}: {game.status}, день {game.day_number}")
    await edit_or_answer(callback, "\n".join(lines), admin_games_kb(games))


@router.callback_query(AdminCB.filter(F.action == "endgame"))
async def cb_admin_endgame(callback: CallbackQuery, callback_data: AdminCB, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    game = await GameRepository(session).get(int(callback_data.value))
    if game is None:
        await edit_or_answer(callback, "Игра не найдена.", admin_back_kb())
        return
    await edit_or_answer(
        callback,
        f"🏁 Завершить игру #{game.id} принудительно?\nИгроки получат уведомление, рейтинг не изменится.",
        admin_confirm_end_kb(game.id),
    )


@router.callback_query(AdminCB.filter(F.action == "endgame_yes"))
async def cb_admin_endgame_yes(callback: CallbackQuery, callback_data: AdminCB, services) -> None:
    if not await _guard(callback):
        return
    done = await services.phases.force_end(int(callback_data.value), "Завершено администратором")
    await callback.answer("Завершена" if done else "Игра не активна", show_alert=True)
    await edit_or_answer(callback, "✅ Игра завершена." if done else "Игра не активна.", admin_back_kb())


@router.callback_query(AdminCB.filter(F.action == "rooms"))
async def cb_admin_rooms(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    rooms = await RoomRepository(session).list_open(20)
    if not rooms:
        await edit_or_answer(callback, "🏠 Активных комнат нет.", admin_back_kb())
        return
    lines = ["🏠 <b>КОМНАТЫ</b>", ""]
    for room in rooms:
        lines.append(
            f"• #{room.id} «{esc(room.name)}» [{room.status}] — 👥 {room.player_count()}/{room.max_players}"
        )
    await edit_or_answer(callback, "\n".join(lines), admin_rooms_kb(rooms))


@router.callback_query(AdminCB.filter(F.action == "closeroom"))
async def cb_admin_close_room(callback: CallbackQuery, callback_data: AdminCB, session) -> None:
    if not await _guard(callback):
        return
    rooms = RoomRepository(session)
    room = await rooms.get(int(callback_data.value))
    if room is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    if room.status == "PLAYING":
        await callback.answer("Нельзя закрыть комнату с активной игрой", show_alert=True)
        return
    room.status = "CLOSED"
    await session.commit()
    await callback.answer("Комната закрыта")
    await cb_admin_rooms(callback, AdminCB(action="rooms"), session)


# ------------------------------------------------------------------- баны

@router.callback_query(AdminCB.filter(F.action == "ban"))
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.ban_input)
    await edit_or_answer(callback, "🚫 Пришли Telegram ID пользователя для бана:")


@router.message(AdminStates.ban_input)
async def process_ban(message: Message, state: FSMContext, session) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз.")
        return
    await state.clear()
    user = await UserRepository(session).set_banned(int(raw), True)
    await session.commit()
    await message.answer(
        "🚫 Забанен." if user else "Пользователь не найден в БД.", reply_markup=admin_back_kb()
    )


@router.callback_query(AdminCB.filter(F.action == "unban"))
async def cb_admin_unban(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.unban_input)
    await edit_or_answer(callback, "✅ Пришли Telegram ID пользователя для разбана:")


@router.message(AdminStates.unban_input)
async def process_unban(message: Message, state: FSMContext, session) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз.")
        return
    await state.clear()
    user = await UserRepository(session).set_banned(int(raw), False)
    await session.commit()
    await message.answer(
        "✅ Разбанен." if user else "Пользователь не найден в БД.", reply_markup=admin_back_kb()
    )


# --------------------------------------------------------------- рассылка

@router.callback_query(AdminCB.filter(F.action == "broadcast"))
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.broadcast_input)
    await edit_or_answer(
        callback,
        "📣 Пришли текст рассылки (HTML разрешён).\n/cancel — отмена.",
    )


@router.message(Command("cancel"), AdminStates.broadcast_input)
@router.message(Command("cancel"), AdminStates.param_input)
async def admin_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_back_kb())


@router.message(AdminStates.broadcast_input)
async def process_broadcast(message: Message, state: FSMContext, session, services) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = message.html_text or (message.text or "")
    await state.clear()
    ids = await UserRepository(session).ids_for_broadcast()
    await message.answer(f"📣 Рассылка {len(ids)} пользователям...")
    ok, failed = await services.notifier.broadcast(ids, text)
    await message.answer(f"📣 Готово: доставлено {ok}, ошибок {failed}.", reply_markup=admin_back_kb())


# ------------------------------------------------------------------- логи

@router.callback_query(AdminCB.filter(F.action == "logs"))
async def cb_admin_logs(callback: CallbackQuery) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    from bot.config import get_settings

    tail = read_log_tail(get_settings().log_file, lines=40)
    await edit_or_answer(callback, f"📜 <b>ЛОГИ (последние строки)</b>\n\n<code>{esc(tail)}</code>", admin_back_kb())


# ------------------------------------------------------------------- роли

@router.callback_query(AdminCB.filter(F.action == "roles"))
async def cb_admin_roles(callback: CallbackQuery, services) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    gs = await services.app_config.get()
    enabled = {r.id for r in gs.enabled_role_objects()}
    await edit_or_answer(
        callback,
        "🎭 <b>УПРАВЛЕНИЕ РОЛЯМИ</b>\n\n✅ — роль доступна в настройках комнат, ⛔ — скрыта.",
        admin_roles_kb(enabled),
    )


@router.callback_query(AdminCB.filter(F.action == "roletoggle"))
async def cb_admin_role_toggle(callback: CallbackQuery, callback_data: AdminCB, services) -> None:
    if not await _guard(callback):
        return
    gs = await services.app_config.get()
    role_id = callback_data.value
    current = {r.id for r in gs.enabled_role_objects()}
    if role_id in current:
        current.discard(role_id)
    else:
        current.add(role_id)
    gs.enabled_roles = sorted(current)
    await services.app_config.save(gs)
    await callback.answer("Обновлено")
    await cb_admin_roles(callback, services)


# ------------------------------------------------------- глобальные параметры

@router.callback_query(AdminCB.filter(F.action == "gparams"))
async def cb_admin_params(callback: CallbackQuery, services) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    gs = await services.app_config.get()
    await edit_or_answer(
        callback,
        "⚙️ <b>ГЛОБАЛЬНЫЕ ПАРАМЕТРЫ</b> (по умолчанию для новых комнат)\n\n"
        f"🌙 Ночь: {gs.night_seconds} сек\n"
        f"☀️ День: {gs.day_seconds} сек\n"
        f"🗳 Голосование: {gs.vote_seconds} сек",
        admin_params_kb(),
    )


@router.callback_query(AdminCB.filter(F.action == "setparam"))
async def cb_admin_setparam(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.param_input)
    await state.update_data(param=callback_data.value)
    await edit_or_answer(callback, f"Введи новое значение параметра <b>{callback_data.value}</b> (секунды, 30–600):")


@router.message(AdminStates.param_input)
async def process_param(message: Message, state: FSMContext, services) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    param = data.get("param")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (30 <= int(raw) <= 600):
        await message.answer("Нужно число 30–600. Попробуй ещё раз.")
        return
    await state.clear()
    gs: GlobalSettings = await services.app_config.get()
    setattr(gs, param, int(raw))
    await services.app_config.save(gs)
    await message.answer(f"✅ {param} = {raw} сек.", reply_markup=admin_back_kb())
