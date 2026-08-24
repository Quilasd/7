"""Распределение ролей и проверка условий победы."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from bot.database.models import GamePlayer, WinningSide
from bot.roles import Team, roles_registry, team_of as _team_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WinResult:
    side: WinningSide
    winner_user_ids: frozenset[int]  # users.id всех победителей (включая погибших)
    title: str                       # «🔵 ПОБЕДИЛИ МИРНЫЕ!»


@dataclass
class DistributionResult:
    roles_by_user: dict[int, str] = field(default_factory=dict)  # user_id -> role_id


def validate_setup(setup: dict[str, int], max_players: int, min_players: int) -> list[str]:
    """Валидация состава ролей комнаты. Возвращает список ошибок (пустой = ок)."""
    errors: list[str] = []
    registry = roles_registry()

    for role_id, count in setup.items():
        if role_id not in registry:
            errors.append(f"Неизвестная роль: {role_id}")
        if count < 0:
            errors.append(f"Количество ролей «{role_id}» не может быть отрицательным")

    total = sum(c for c in setup.values() if c > 0)
    if setup.get("mafia", 0) < 1:
        errors.append("Нужна хотя бы одна 🔴 мафия")
    if setup.get("citizen", 0) not in (0,):
        errors.append("Количество мирных задаётся автоматически (остаток игроков)")
    if total > max_players:
        errors.append(f"Сумма ролей ({total}) больше максимума игроков ({max_players})")
    if total >= max_players:
        errors.append("Должен остаться хотя бы один слот для мирных (роль по умолчанию)")
    if min_players > max_players:
        errors.append("Минимум игроков не может быть больше максимума")
    return errors


def distribute_roles(user_ids: list[int], setup: dict[str, int], rng: random.Random | None = None) -> DistributionResult:
    """Случайное распределение ролей.

    Правила:
    - роли из setup раздаются в случайном порядке;
    - все, кому не досталось роли, получают «citizen»;
    - гарантирована уникальность (каждый игрок — одна роль).
    """
    rng = rng or random.Random()
    shuffled = list(user_ids)
    rng.shuffle(shuffled)

    result = DistributionResult()
    index = 0
    for role_id, count in sorted(setup.items()):
        if role_id == "citizen" or count <= 0:
            continue
        for _ in range(count):
            if index >= len(shuffled):
                break  # защищено валидацией, но на всякий случай
            result.roles_by_user[shuffled[index]] = role_id
            index += 1

    for user_id in shuffled[index:]:
        result.roles_by_user[user_id] = "citizen"
    return result


def team_of(role_id: str | None) -> Team:
    """Публичный алиас (реэкспорт из bot.roles)."""
    return _team_of(role_id)


def evaluate_win(players: list[GamePlayer]) -> WinResult | None:
    """Проверка условий победы. None — игра продолжается.

    Приоритет проверки: ничья -> мафия -> маньяк -> город.
    Решение: проверять мафию до маньяка (паритет мафии «сильнее»).
    """
    alive = [p for p in players if p.is_alive]
    if not alive:
        return WinResult(WinningSide.DRAW, frozenset(), "🤝 НИЧЬЯ")

    def side_players(team: Team) -> list[GamePlayer]:
        return [p for p in players if p.status != "SPECTATOR" and team_of(p.role) == team]

    mafia_alive = [p for p in alive if team_of(p.role) == Team.MAFIA]
    neutral_alive = [p for p in alive if team_of(p.role) == Team.NEUTRAL]
    others_alive = len(alive) - len(mafia_alive)

    if mafia_alive and len(mafia_alive) >= others_alive:
        return WinResult(
            WinningSide.MAFIA,
            frozenset(p.user_id for p in side_players(Team.MAFIA)),
            "🔴 ПОБЕДИЛА МАФИЯ!",
        )

    if neutral_alive and len(alive) <= 2:
        return WinResult(
            WinningSide.MANIAC,
            frozenset(p.user_id for p in side_players(Team.NEUTRAL)),
            "🔪 ПОБЕДИЛ МАНЬЯК!",
        )

    if not mafia_alive and not neutral_alive:
        return WinResult(
            WinningSide.CITY,
            frozenset(p.user_id for p in side_players(Team.CITY)),
            "🔵 ПОБЕДИЛИ МИРНЫЕ!",
        )

    return None
