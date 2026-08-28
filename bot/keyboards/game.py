"""Игровые клавиатуры: ночные действия, голосование, статус."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database.models import Game, GamePlayer, GameStatus
from bot.roles import Role
from bot.utils.callbacks import GameCB, NightConfirmCB, NightCB, VoteConfirmCB, VoteCB
from bot.utils.helpers import display_name


def night_action_keyboard(
    game_id: int, role: Role, targets: list[GamePlayer], actor: GamePlayer
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for target in targets:
        rows.append([
            InlineKeyboardButton(
                text=display_name(target.user),
                callback_data=NightCB(
                    game_id=game_id, action=role.night_action.value, target_id=target.user_id
                ).pack(),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def night_confirm_keyboard(game_id: int, action: str, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=NightConfirmCB(game_id=game_id, action=action, target_id=target_id, op="yes").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=NightConfirmCB(game_id=game_id, action=action, target_id=target_id, op="no").pack(),
        ),
    ]])


def vote_keyboard(
    game_id: int, round_no: int, candidates: list[GamePlayer], voter: GamePlayer
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for candidate in candidates:
        if candidate.user_id == voter.user_id:
            continue  # за себя голосовать нельзя
        rows.append([
            InlineKeyboardButton(
                text=display_name(candidate.user),
                callback_data=VoteCB(
                    game_id=game_id, round_no=round_no, target_id=candidate.user_id
                ).pack(),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vote_confirm_keyboard(game_id: int, round_no: int, target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=VoteConfirmCB(game_id=game_id, round_no=round_no, target_id=target_id, op="yes").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=VoteConfirmCB(game_id=game_id, round_no=round_no, target_id=target_id, op="no").pack(),
        ),
    ]])


def game_status_keyboard(game: Game, game_player: GamePlayer) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    role_actions = {
        GameStatus.NIGHT.value: "🌙 Ночное действие",
        GameStatus.VOTING.value: "🗳 Проголосовать",
    }
    if game.status in role_actions and game_player.is_alive:
        rows.append([
            InlineKeyboardButton(
                text=role_actions[game.status],
                callback_data=GameCB(
                    action="night" if game.status == GameStatus.NIGHT.value else "revote_ui",
                    game_id=game.id,
                ).pack(),
            )
        ])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=GameCB(action="status", game_id=game.id).pack())
    ])
    if game.status in (GameStatus.NIGHT.value, GameStatus.DAY.value, GameStatus.VOTING.value, GameStatus.STARTING.value) and game_player.is_alive:
        rows.append([
            InlineKeyboardButton(
                text="🚪 Покинуть игру",
                callback_data=GameCB(action="leave", game_id=game.id).pack(),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def death_note_keyboard(game_id: int) -> InlineKeyboardMarkup:
    """Кнопка для написания предсмертной записки (показывается умершему)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📝 Написать предсмертную записку",
            callback_data=GameCB(action="note", game_id=game_id).pack(),
        )
    ]])
