"""Игровые хендлеры: состояние игры, ночные действия, выход из игры."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.keyboards.game import (
    game_status_keyboard,
    night_action_keyboard,
    night_confirm_keyboard,
)
from bot.keyboards.room import confirm_kb
from bot.roles import get_role
from bot.states import NoteStates
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


# -------------------------------------------------------- предсмертная записка

DEATH_NOTE_MAX = 300


async def _save_note(session, game_id: int, user_id: int, text: str) -> tuple[bool, str]:
    from bot.database.repositories.social import DeathNoteRepository

    text = (text or "").strip()
    if len(text) > DEATH_NOTE_MAX:
        return False, f"Слишком длинно — до {DEATH_NOTE_MAX} символов."
    note = await DeathNoteRepository(session).set_text(game_id, user_id, text)
    if note is None:
        return False, "Записку нельзя оставить: ты ещё не выбыл или уже написал её."
    if not text:
        return True, "☠️ Принято: ты ничего не успел сказать. Записка будет нейтральной."
    return True, "📝 Предсмертная записка сохранена. Её прочтут утром (один раз, изменить нельзя)."


@router.callback_query(GameCB.filter(F.action == "note"))
async def cb_note(callback: CallbackQuery, callback_data: GameCB, state, services, db_user) -> None:
    """Кнопка «написать записку» — переводит в режим ввода текста."""
    from bot.states import NoteStates

    async with services.session_factory() as session:
        from bot.database.repositories.social import DeathNoteRepository
        note = await DeathNoteRepository(session).get(callback_data.game_id, db_user.id)
    if note is None:
        await callback.answer("Ты не выбыл из этой игры — записка недоступна.", show_alert=True)
        return
    if note.text is not None:
        await callback.answer("Ты уже написал записку — её нельзя изменить.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(NoteStates.text)
    await state.update_data(game_id=callback_data.game_id)
    await edit_or_answer(
        callback,
        f"📝 Напиши предсмертную записку (до {DEATH_NOTE_MAX} символов).\n"
        "Опубликуется утром. Один раз, изменить нельзя.\n\n/cancel — отменить.",
    )


@router.message(Command("note"))
async def cmd_note(message: Message, command, session, services, db_user) -> None:
    """/note <текст> — записка для текущей активной игры (умерший игрок)."""
    text = (command.args or "").strip()
    active = await GamePlayerRepository(session).active_game_of_user(db_user.id)
    if active is None:
        await message.answer("У тебя нет активной игры для записки.")
        return
    if not text:
        await message.answer(
            f"📝 Напиши записку так: <code>/note твой текст</code> (до {DEATH_NOTE_MAX} символов). "
            "Либо отправь пустую, если не хочешь ничего говорить."
        )
        return
    ok, msg = await _save_note(session, active.game_id, db_user.id, text)
    if ok:
        await session.commit()
    await message.answer(msg)


@router.message(NoteStates.text)
async def process_note(message: Message, state: FSMContext, session, db_user) -> None:
    data = await state.get_data()
    game_id = data.get("game_id")
    if not game_id:
        await state.clear()
        return
    ok, msg = await _save_note(session, int(game_id), db_user.id, message.text or "")
    if ok:
        await session.commit()
    await state.clear()
    await message.answer(msg)


@router.message(Command("cancel"), NoteStates.text)
async def cb_note_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Записка отменена. Написать можно позже командой /note.")
