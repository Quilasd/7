"""Рейтинг и XP: глобальный (User) и локальный (GroupPlayer) — физически разделены.

RatingService — единственное место с формулами:
- рейтинг: победа +100, поражение +25, плюс бонусы за вклад
  (убийства/спасения/расследования/правильные голосования/выживание);
- XP: участие +10, победа +25, убийство +5, спасение +5, расследование +3,
  правильное голосование +2, выживание +5.

Значения собраны в RatingRules — меняются одной структурой.
Антифарм: при ничьей/принудительной остановке XP и рейтинг НЕ начисляются
(только games_played), а статистика применяется только в _end_game
нормально завершённой игры.

Скоупы применяются независимо:
- GLOBAL -> строки User (глобальные поля);
- GROUP  -> строки GroupPlayer ТОЛЬКО конкретной группы.

Формулы НЕ живут в хендлерах: хендлеры лишь вызывают RatingService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from bot.services.progression import ProgressionService

logger = logging.getLogger(__name__)


class RatingScope(str, Enum):
    GLOBAL = "global"
    GROUP = "group"


@dataclass
class StatEvents:
    """Личный вклад за игру: user_id -> счётчики."""

    kills: dict[int, int] = field(default_factory=dict)
    saves: dict[int, int] = field(default_factory=dict)
    investigations: dict[int, int] = field(default_factory=dict)
    correct_votes: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RatingRules:
    # Рейтинг
    rating_win: int = 100
    rating_loss: int = 25
    rating_per_kill: int = 5
    rating_per_save: int = 5
    rating_per_investigation: int = 3
    rating_per_correct_vote: int = 2
    rating_survival: int = 10
    # XP
    xp_participation: int = 10
    xp_win: int = 25
    xp_per_kill: int = 5
    xp_per_save: int = 5
    xp_per_investigation: int = 3
    xp_per_correct_vote: int = 2
    xp_survival: int = 5


DEFAULT_RULES = RatingRules()


@dataclass
class ScopeFlags:
    """Какие показатели включены (из GroupSettings + DEBUG-флагов)."""

    rating: bool = True
    xp: bool = True


@dataclass
class AppliedStats:
    rating_delta: dict[int, int] = field(default_factory=dict)
    xp_delta: dict[int, int] = field(default_factory=dict)


def contribution(rules: RatingRules, user_id: int, events: StatEvents, survived: bool) -> tuple[int, int]:
    """(дельта рейтинга за вклад, дельта XP за вклад)."""
    rating = (
        events.kills.get(user_id, 0) * rules.rating_per_kill
        + events.saves.get(user_id, 0) * rules.rating_per_save
        + events.investigations.get(user_id, 0) * rules.rating_per_investigation
        + events.correct_votes.get(user_id, 0) * rules.rating_per_correct_vote
        + (rules.rating_survival if survived else 0)
    )
    xp = (
        events.kills.get(user_id, 0) * rules.xp_per_kill
        + events.saves.get(user_id, 0) * rules.xp_per_save
        + events.investigations.get(user_id, 0) * rules.xp_per_investigation
        + events.correct_votes.get(user_id, 0) * rules.xp_per_correct_vote
        + (rules.xp_survival if survived else 0)
    )
    return rating, xp


class RatingService:
    """Применение результатов игры к глобальной и/или локальной статистике.

    apply_global(...) меняет ТОЛЬКО User; apply_local(...) меняет ТОЛЬКО
    GroupPlayer конкретной группы. Методы идентичны по формулам, но работают
    с разными объектами — случайная перезапись невозможна по построению.
    """

    def __init__(
        self,
        rules: RatingRules = DEFAULT_RULES,
        progression: ProgressionService = ProgressionService(),
    ) -> None:
        self.rules = rules
        self.progression = progression

    def _apply_rows(
        self,
        rows_by_user_id: dict[int, object],
        winners: set[int],
        is_draw: bool,
        events: StatEvents,
        survived_ids: set[int],
        scope: ScopeFlags,
    ) -> AppliedStats:
        """Общий алгоритм над строками с полями статистики (User | GroupPlayer)."""
        applied = AppliedStats()
        for user_id, row in rows_by_user_id.items():
            if row is None:
                continue
            won = user_id in winners and not is_draw
            survived = user_id in survived_ids
            contrib_rating, contrib_xp = contribution(self.rules, user_id, events, survived)

            rating_delta = 0
            xp_delta = 0
            if is_draw:
                pass  # антифарм: ничья ничего не начисляет
            else:
                if scope.rating:
                    rating_delta = (self.rules.rating_win if won else self.rules.rating_loss) + contrib_rating
                if scope.xp:
                    xp_delta = (
                        self.rules.xp_participation
                        + (self.rules.xp_win if won else 0)
                        + contrib_xp
                    )

            row.games_played += 1
            if not is_draw:
                if won:
                    row.wins += 1
                    # 🔥 серия побед: победа продлевает, лучшая фиксируется
                    row.win_streak = int(getattr(row, "win_streak", 0) or 0) + 1
                    row.best_win_streak = max(
                        int(getattr(row, "best_win_streak", 0) or 0), row.win_streak
                    )
                else:
                    row.losses += 1
                    # поражение сбрасывает текущую серию (лучшая остаётся)
                    row.win_streak = 0
            if scope.rating:
                row.rating = max(0, row.rating + rating_delta)
            if scope.xp:
                row.xp = max(0, row.xp + xp_delta)
                row.level = self.progression.level_for_xp(row.xp)

            # Счётчики личного вклада (идентичны в обоих скоупах)
            row.kills += events.kills.get(user_id, 0)
            row.saves += events.saves.get(user_id, 0)
            row.investigations += events.investigations.get(user_id, 0)
            row.correct_votes += events.correct_votes.get(user_id, 0)

            applied.rating_delta[user_id] = rating_delta
            applied.xp_delta[user_id] = xp_delta
        return applied

    # ------------------------------------------------------------- скоупы

    def apply_global(
        self,
        users_by_id: dict[int, object],
        winners: set[int],
        is_draw: bool,
        events: StatEvents,
        survived_ids: set[int],
        flags: ScopeFlags,
    ) -> AppliedStats:
        """Глобальная статистика: ТОЛЬКО объекты User."""
        return self._apply_rows(users_by_id, winners, is_draw, events, survived_ids, flags)

    def apply_local(
        self,
        group_players_by_user_id: dict[int, object],
        winners: set[int],
        is_draw: bool,
        events: StatEvents,
        survived_ids: set[int],
        flags: ScopeFlags,
    ) -> AppliedStats:
        """Локальная статистика: ТОЛЬКО GroupPlayer конкретной группы."""
        return self._apply_rows(
            group_players_by_user_id, winners, is_draw, events, survived_ids, flags
        )
