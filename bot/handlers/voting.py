"""Голосование: выбор кандидата, подтверждение, переголосование."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.keyboards.game import vote_confirm_keyboard, vote_keyboard
from bot.utils.callbacks import GameCB, VoteCB, VoteConfirmCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(GameCB.filter(F.action == "revote_ui"))
async def cb_vote_ui(callback: CallbackQuery, callback_data: GameCB, services, db_user) -> None:
    """Показать кнопки голосования (повторно, по кнопке из статуса)."""
    result = await services.games.get_status(callback_data.game_id, db_user.id)
    if not result.ok:
        await callback.answer(result.message[:180], show_alert=True)
        return
    async with services.session_factory() as session:
        game = await GameRepository(session).get(callback_data.game_id)
        if game is None or game.status != "VOTING":
            await callback.answer("Сейчас не фаза голосования", show_alert=True)
            return
        players = await GamePlayerRepository(session).list_for_game(game.id)
        gp = next((p for p in players if p.user_id == db_user.id), None)
        if gp is None or not gp.is_alive:
            await callback.answer("Голосуют только живые игроки", show_alert=True)
            return
        from bot.services.vote_manager import VoteManager

        round_no = VoteManager.current_round(game)
        candidates = VoteManager.candidates(game)
        alive = [p for p in players if p.is_alive]
        cand_ids = candidates if candidates is not None else [p.user_id for p in alive]
        cand_players = [p for p in alive if p.user_id in cand_ids]
    from bot.services.game_view import voting_text
    from bot.utils.helpers import deadline_in

    await callback.answer()
    await edit_or_answer(
        callback,
        voting_text(game, cand_players, deadline_in(game.phase_deadline), round_no),
        vote_keyboard(game.id, round_no, cand_players, gp),
    )


@router.callback_query(VoteCB.filter())
async def cb_vote_select(callback: CallbackQuery, callback_data: VoteCB, services, db_user) -> None:
    async with services.session_factory() as session:
        game = await GameRepository(session).get(callback_data.game_id)
        if game is None or game.status != "VOTING":
            await callback.answer("Голосование завершено", show_alert=True)
            return
        target = await GamePlayerRepository(session).get_by_user(game.id, callback_data.target_id)
    if target is None or not target.is_alive:
        await callback.answer("Этот игрок уже выбыл", show_alert=True)
        return
    await callback.answer()
    await edit_or_answer(
        callback,
        f"🗳 Отдать голос за <b>{esc(display_name(target.user))}</b>?",
        vote_confirm_keyboard(game.id, callback_data.round_no, callback_data.target_id),
    )


@router.callback_query(VoteConfirmCB.filter(F.op == "yes"))
async def cb_vote_confirm(callback: CallbackQuery, callback_data: VoteConfirmCB, services, db_user) -> None:
    result = await services.games.cast_vote(
        game_id=callback_data.game_id,
        voter_user_id=db_user.id,
        target_user_id=callback_data.target_id,
    )
    await callback.answer("Принято" if result.ok else result.message[:180], show_alert=not result.ok)
    await edit_or_answer(
        callback,
        result.message if result.ok else f"⚠️ {esc(result.message)}",
    )


@router.callback_query(VoteConfirmCB.filter(F.op == "no"))
async def cb_vote_cancel(
    callback: CallbackQuery, callback_data: VoteConfirmCB, services, db_user
) -> None:
    await callback.answer("Отменено")
    await cb_vote_ui(
        callback, GameCB(action="revote_ui", game_id=callback_data.game_id), services, db_user
    )
