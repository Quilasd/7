"""Система достижений (реестр + оценка по итогам партии).

Достижения привязаны к ИГРОВЫМ ситуациям (не к гринду). Определения живут в
коде (реестр ниже); факты получения — в БД (user_achievements, одноразовые).

Расширение: добавь Achievement в _ALL_ACHIEVEMENTS и (если нужна новая
детекция) условие в evaluate(). Титулы, открываемые достижениями, см.
в bot/services/titles.py (TITLE_UNLOCKS).

Контекст оценки (GameAchievementContext) собирается в PhaseManager._end_game
из уже посчитанных событий партии — дублирующего хранилища нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from bot.roles import Team, get_role, team_of


@dataclass(frozen=True)
class Achievement:
    id: str
    name: str
    emoji: str
    description: str
    hidden: bool = False  # скрытые — видны только после получения


@dataclass
class GameAchievementContext:
    """Снимок партии для оценки достижений (user_id-ориентированный)."""

    # user_id -> role_id
    roles: dict[int, str] = field(default_factory=dict)
    winners: set[int] = field(default_factory=set)
    is_draw: bool = False
    survived_ids: set[int] = field(default_factory=set)
    # личный вклад за партию
    kills: dict[int, int] = field(default_factory=dict)             # убийства (мафия/маньяк)
    saves: dict[int, int] = field(default_factory=dict)             # спасения (доктор/телохранитель)
    correct_checks: dict[int, int] = field(default_factory=dict)    # верные проверки комиссара
    correct_votes: dict[int, int] = field(default_factory=dict)     # голос за мафию мирным
    sacrificed_ids: set[int] = field(default_factory=set)           # телохранители, погибшие спасая
    maniac_killers: set[int] = field(default_factory=set)           # маньяки, совершившие убийство
    # состояние до/после (для «первая победа», серия)
    wins_before: dict[int, int] = field(default_factory=dict)
    win_streak_after: dict[int, int] = field(default_factory=dict)


# ------------------------------------------------------------------ реестр


def A(id, name, emoji, description, hidden=False):  # noqa: ANN001 - компактно
    return Achievement(id, name, emoji, description, hidden)


_ALL_ACHIEVEMENTS: list[Achievement] = [
    A("first_win", "Первая кровь", "🎉", "Одержать свою первую победу."),
    A("savior", "Спаситель", "❤️", "Доктором спасти игрока от смерти."),
    A("shield", "Живой щит", "🛡️", "Телохранителем принять удар вместо другого игрока."),
    A("detective_accurate", "Верная догадка", "🕵️", "Комиссаром правильно вычислить мафию."),
    A("hunter", "Охотник", "🔪", "Маньяком успешно убить игрока."),
    A("sharp_eye", "Верный приговор", "🎯", "Городом правильно проголосовать против мафии."),
    A("last_survivor", "Последний выживший", "👑", "Победить, оставшись единственным живым."),
    A("mafia_win", "Идеальное алиби", "🧠", "Победить в составе мафии."),
    A("city_win", "Город встал", "🔵", "Победить в составе мирных."),
    A("unstoppable", "Неудержимый", "🔥", "Собрать серию из 5 побед подряд."),
    A("legendary_streak", "Легенда", "🏆", "Собрать серию из 10 побед подряд."),
    A("sharpshooter", "Снайпер", "🔫", "Мафией убить 3 и более игроков за партию.", hidden=True),
]

_REGISTRY: dict[str, Achievement] = {a.id: a for a in _ALL_ACHIEVEMENTS}


def get_achievement(achievement_id: str) -> Achievement | None:
    return _REGISTRY.get(achievement_id)


def all_achievements() -> list[Achievement]:
    return list(_ALL_ACHIEVEMENTS)


def total_achievements() -> int:
    return len(_ALL_ACHIEVEMENTS)


# ------------------------------------------------------------------ оценка


def _alive_count(ctx: GameAchievementContext) -> int:
    return len(ctx.survived_ids)


def evaluate(ctx: GameAchievementContext) -> dict[int, set[str]]:
    """Возвращает {user_id: {achievement_id}} — заработанные в партии.

    Чистая функция над снимком: не трогает БД. Вызывающий код фиксирует
    новые достижения через UserAchievementRepository (одноразово).
    """
    earned: dict[int, set[str]] = {uid: set() for uid in ctx.roles}

    def award(uid: int, aid: str) -> None:
        earned.setdefault(uid, set()).add(aid)

    last_survivor_uid = None
    if len(ctx.survived_ids) == 1:
        last_survivor_uid = next(iter(ctx.survived_ids))

    for uid, role_id in ctx.roles.items():
        team = team_of(role_id)
        won = uid in ctx.winners and not ctx.is_draw
        wins_before = ctx.wins_before.get(uid, 0)

        # первая победа
        if won and wins_before == 0:
            award(uid, "first_win")
        # вклад по роли
        if ctx.saves.get(uid, 0) > 0 and team == Team.CITY:
            award(uid, "savior")
        if uid in ctx.sacrificed_ids:
            award(uid, "shield")
        if ctx.correct_checks.get(uid, 0) > 0 and team == Team.CITY:
            award(uid, "detective_accurate")
        if uid in ctx.maniac_killers:
            award(uid, "hunter")
        if ctx.correct_votes.get(uid, 0) > 0 and team == Team.CITY:
            award(uid, "sharp_eye")
        if ctx.kills.get(uid, 0) >= 3 and team == Team.MAFIA:
            award(uid, "sharpshooter")
        # победа стороной
        if won and team == Team.MAFIA:
            award(uid, "mafia_win")
        if won and team == Team.CITY:
            award(uid, "city_win")
        # последний выживший (победитель, остался один)
        if won and last_survivor_uid == uid:
            award(uid, "last_survivor")
        # серии побед (по итогу после применения рейтинга)
        streak = ctx.win_streak_after.get(uid, 0)
        if streak >= 5:
            award(uid, "unstoppable")
        if streak >= 10:
            award(uid, "legendary_streak")

    return earned


__all__ = [
    "Achievement", "GameAchievementContext", "get_achievement",
    "all_achievements", "total_achievements", "evaluate",
]
