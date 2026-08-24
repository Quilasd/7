"""Игровые хендлеры: состояние игры, ночные действия, выход из игры."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.keyboards.game import (
    game_status_keyboard,
    night_action_keyboard,
    night_confirm_keyboard,
)
from bot.keyboards.room import confirm_kb
from bot.roles import get_role
from bot.utils.callbacks import GameCB, NightCB, NightConfirmCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


async def _load_game_and_player(services, game_id: int, user_id: int):
    async with services.session_factory() as session:
        game = await GameRepository(session).get(game_id)
        if game is None:
            return None, None
        gp = await GamePlayerRepository(session).get_by_user(game.id, user_id)
        return game, gp


@router.callback_query(GameCB.filter(F.action == "status"))
async def cb_game_status(callback: CallbackQuery, callback_data: GameCB, services, db_user) -> None:
    result = await services.games.get_status(callback_data.game_id, db_user.id)
    if not result.ok:
        await callback.answer(result.message[:180], show_alert=True)
        return
    await callback.answer()
    game, gp = await _load_game_and_player(services, callback_data.game_id, db_user.id)
    if game is None or gp is None:
        await edit_or_answer(callback, esc(result.message))
        return
    await edit_or_answer(
        callback, result.message, game_status_keyboard(game, gp)
    )


async def _send_night_action_ui(
    callback: CallbackQuery, services, game_id: int, db_user
) -> None:
    """Показывает кнопки выбора цели для ночного действия игрока."""
    game, gp = await _load_game_and_player(services, game_id, db_user.id)
    if game is None or gp is None:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    role = get_role(gp.role)
    if game.status != "NIGHT":
        await callback.answer("Сейчас не ночь", show_alert=True)
        return
    if not gp.is_alive or role is None or role.night_action is None:
        await callback.answer("У тебя нет доступных ночных действий", show_alert=True)
        return
    async with services.session_factory() as session:
        # Валидные цели считает PhaseManager (правила роли + история)
        players = await GamePlayerRepository(session).list_for_game(game.id)
        targets = await services.phases.valid_night_targets(session, game, gp, players)
    if not targets:
        await callback.answer("Нет доступных целей", show_alert=True)
        return
    keyboard = night_action_keyboard(game.id, role, targets, gp)
    await callback.answer()
    await edit_or_answer(
        callback,
        f"{role.emoji} <b>{role.action_prompt}</b>\n\nВыбери игрока:",
        keyboard,
    )


@router.callback_query(GameCB.filter(F.action == "night"))
async def cb_night_ui(callback: CallbackQuery, callback_data: GameCB, services, db_user) -> None:
    await _send_night_action_ui(callback, services, callback_data.game_id, db_user)


@router.callback_query(NightCB.filter())
async def cb_night_select(
    callback: CallbackQuery, callback_data: NightCB, services, db_user
) -> None:
    """Выбор цели -> экран подтверждения (двухшаговый, защищает от мискликов)."""
    game, gp = await _load_game_and_player(services, callback_data.game_id, db_user.id)
    if game is None or gp is None:
        await callback.answer("Игра не найдена", show_alert=True)
        return
    if game.status != "NIGHT":
        await callback.answer("Сейчас не ночь — действие отменено", show_alert=True)
        return
    async with services.session_factory() as session:
        target = await GamePlayerRepository(session).get_by_user(game.id, callback_data.target_id)
    if target is None or not target.is_alive:
        await callback.answer("Эта цель больше недоступна", show_alert=True)
        return
    role = get_role(gp.role)
    await callback.answer()
    await edit_or_answer(
        callback,
        f"{role.emoji} {role.action_verb}: <b>{esc(display_name(target.user))}</b>?\n"
        "Действие можно будет изменить до конца ночи.",
        night_confirm_keyboard(game.id, callback_data.action, callback_data.target_id),
    )


@router.callback_query(NightConfirmCB.filter(F.op == "yes"))
async def cb_night_confirm(
    callback: CallbackQuery, callback_data: NightConfirmCB, services, db_user
) -> None:
    result = await services.games.submit_night_action(
        game_id=callback_data.game_id,
        actor_user_id=db_user.id,
        action_type=callback_data.action,
        target_user_id=callback_data.target_id,
    )
    await callback.answer("Принято" if result.ok else result.message[:180], show_alert=not result.ok)
    if result.ok:
        await edit_or_answer(callback, result.message)
    else:
        await edit_or_answer(callback, f"⚠️ {esc(result.message)}")


@router.callback_query(NightConfirmCB.filter(F.op == "no"))
async def cb_night_cancel(callback: CallbackQuery, callback_data: NightConfirmCB, services, db_user) -> None:
    await callback.answer("Отменено")
    await _send_night_action_ui(callback, services, callback_data.game_id, db_user)


# ------------------------------------------------------------------- выход

@router.callback_query(GameCB.filter(F.action == "leave"))
async def cb_game_leave(callback: CallbackQuery, callback_data: GameCB) -> None:
    await callback.answer()
    await edit_or_answer(
        callback,
        "🚪 Покинуть игру? Ты выбудешь из города (это нельзя отменить).",
        confirm_kb(
            GameCB(action="leave_yes", game_id=callback_data.game_id).pack(),
            GameCB(action="status", game_id=callback_data.game_id).pack(),
            yes_text="🚪 Да, покинуть",
        ),
    )


@router.callback_query(GameCB.filter(F.action == "leave_yes"))
async def cb_game_leave_yes(callback: CallbackQuery, callback_data: GameCB, services, db_user) -> None:
    result = await services.games.leave_game(callback_data.game_id, db_user.id)
    await callback.answer(result.message[:180], show_alert=not result.ok)
    from bot.keyboards.common import back_to_menu_kb

    await edit_or_answer(callback, esc(result.message), back_to_menu_kb())
