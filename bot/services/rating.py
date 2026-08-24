"""Рейтинг, уровни и опыт.

Формулы (выбраны простые и объяснимые, см. README):
- победа города/мафии: +25 рейтинга; поражение: −12 (не ниже 0);
- победа маньяка: +40 (соло-победа сложнее);
- XP: 10 за участие, +25 за победу, +5 за каждое убийство, +5 за спасение,
  +2 за правильное голосование;
- уровень — квадратичная шкала (100 XP до 2-го, дальше +50 за уровень).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bot.database.models import User, WinningSide
from bot.utils.helpers import level_from_xp

logger = logging.getLogger(__name__)

WIN_RATING = {WinningSide.CITY: 25, WinningSide.MAFIA: 25, WinningSide.MANIAC: 40}
LOSS_RATING = -12
XP_PARTICIPATION = 10
XP_WIN = 25
XP_KILL = 5
XP_SAVE = 5
XP_CORRECT_VOTE = 2


@dataclass
class StatEvents:
    """События игры для начисления личной статистики."""

    kills: dict[int, int] = field(default_factory=dict)          # user_id -> кол-во
    saves: dict[int, int] = field(default_factory=dict)
    correct_votes: dict[int, int] = field(default_factory=dict)


@dataclass
class AppliedStats:
    rating_delta: dict[int, int] = field(default_factory=dict)
    xp_delta: dict[int, int] = field(default_factory=dict)


def apply_game_results(
    users_by_id: dict[int, User],
    winners: set[int],
    side: WinningSide,
    events: StatEvents,
) -> AppliedStats:
    """Обновляет статистику пользователей in-memory (коммит — задача вызывающего)."""
    applied = AppliedStats()

    for user_id, user in users_by_id.items():
        if user is None:
            continue
        is_draw = side in (WinningSide.DRAW,)
        won = user_id in winners and not is_draw

        rating_delta = 0
        if not is_draw:
            rating_delta = WIN_RATING.get(side, 0) if won else LOSS_RATING
        xp_delta = XP_PARTICIPATION
        if won:
            xp_delta += XP_WIN
        xp_delta += events.kills.get(user_id, 0) * XP_KILL
        xp_delta += events.saves.get(user_id, 0) * XP_SAVE
        xp_delta += events.correct_votes.get(user_id, 0) * XP_CORRECT_VOTE

        user.games_played += 1
        if won:
            user.wins += 1
        elif not is_draw:
            user.losses += 1

        user.rating = max(0, user.rating + rating_delta)
        user.xp = max(0, user.xp + xp_delta)
        user.level = level_from_xp(user.xp)
        user.kills += events.kills.get(user_id, 0)
        user.saves += events.saves.get(user_id, 0)
        user.correct_votes += events.correct_votes.get(user_id, 0)

        applied.rating_delta[user_id] = rating_delta
        applied.xp_delta[user_id] = xp_delta

    return applied
