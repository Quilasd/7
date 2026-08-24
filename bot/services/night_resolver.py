"""Обработка ночных действий.

Конвейер строго упорядочен (NIGHT_PIPELINE):
  1. BLOCK   — любовница блокирует игроков: их действия отменяются;
  2. PROTECT — доктор лечит, телохранитель защищает;
  3. KILL    — мафия (консенсус) и маньяк убивают;
  4. CHECK   — комиссар проверяет.

Новая стадия добавляется вставкой в NIGHT_PIPELINE и обработчиком ниже —
без изменения остального движка.

Правила, принятые в этой реализации (см. README):
- blocked-игрок не узнаёт о блокировке;
- «голос» мафии: цель выбирается большинством поданных kill-действий,
  при равенстве — случайно среди лидеров;
- доктор/телохранитель не могут спасать, если их заблокировали;
- телохранитель погибает вместо спасённого (sacrifice_on_save);
- несколько атак по одной цели = одна смерть;
- комиссару маньяк показывается как «не мафия».
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from bot.database.models import GameAction, GamePlayer
from bot.roles import ActionType, Role, Team, get_role

logger = logging.getLogger(__name__)


@dataclass
class NightDeath:
    user_id: int
    cause: str            # mafia | maniac | sacrifice
    killers: list[int] = field(default_factory=list)  # users.id убийц (для статистики)


@dataclass
class CheckOutcome:
    detective_id: int
    target_id: int
    target_is_mafia: bool


@dataclass
class SaveEvent:
    healer_id: int
    target_id: int
    healer_role: str


@dataclass
class NightOutcome:
    deaths: list[NightDeath] = field(default_factory=list)
    checks: list[CheckOutcome] = field(default_factory=list)
    saves: list[SaveEvent] = field(default_factory=list)
    blocked_user_ids: list[int] = field(default_factory=list)


def _role_of(players: list[GamePlayer], user_id: int) -> Role | None:
    for p in players:
        if p.user_id == user_id:
            return get_role(p.role)
    return None


def resolve_night(
    actions: list[GameAction],
    players: list[GamePlayer],
    rng: random.Random | None = None,
) -> NightOutcome:
    """Чистая функция: без БД и Telegram — легко тестируется."""
    rng = rng or random.Random()
    outcome = NightOutcome()

    alive_ids = {p.user_id for p in players if p.is_alive}

    # Оставляем только действия живых актёров (защита от «действия после смерти»)
    valid_actions = [
        a for a in actions if a.actor_id in alive_ids and a.target_id in alive_ids
    ]

    def actor_role(action: GameAction) -> Role | None:
        return _role_of(players, action.actor_id)

    # --- Стадия 1: BLOCK -----------------------------------------------------
    blocked: set[int] = set()
    for action in valid_actions:
        role = actor_role(action)
        if action.action_type == ActionType.BLOCK.value and role and role.night_action == ActionType.BLOCK:
            blocked.add(action.target_id)
    outcome.blocked_user_ids = sorted(blocked)

    def cancelled(action: GameAction) -> bool:
        """Действие отменено, если актёр заблокирован."""
        return action.actor_id in blocked

    active_actions = [a for a in valid_actions if not cancelled(a)]

    # --- Стадия 2: PROTECT ---------------------------------------------------
    protections: dict[int, list[GameAction]] = {}
    for action in active_actions:
        role = actor_role(action)
        if (
            action.action_type == ActionType.PROTECT.value
            and role
            and role.night_action == ActionType.PROTECT
        ):
            protections.setdefault(action.target_id, []).append(action)
        if (
            action.action_type == ActionType.HEAL.value
            and role
            and role.night_action == ActionType.HEAL
        ):
            protections.setdefault(action.target_id, []).append(action)

    # --- Стадия 3: KILL ------------------------------------------------------
    dead: dict[int, NightDeath] = {}

    def try_kill(target_id: int, cause: str, killers: list[int]) -> None:
        if target_id in protections:
            for guard in protections[target_id]:
                role = actor_role(guard)
                if role and role.sacrifice_on_save and guard.actor_id not in dead:
                    # Телохранитель гибнет вместо спасённого
                    dead[guard.actor_id] = NightDeath(guard.actor_id, "sacrifice", [])
            for guard in protections[target_id]:
                outcome.saves.append(
                    SaveEvent(guard.actor_id, target_id, actor_role(guard).id if actor_role(guard) else "doctor")
                )
            return
        if target_id not in dead:
            dead[target_id] = NightDeath(target_id, cause, killers)

    # Мафия: консенсус по поданным kill-действиям членов мафии
    mafia_votes: dict[int, int] = {}
    for action in active_actions:
        role = actor_role(action)
        if (
            action.action_type == ActionType.KILL.value
            and role
            and role.night_action == ActionType.KILL
            and role.team == Team.MAFIA
        ):
            mafia_votes[action.target_id] = mafia_votes.get(action.target_id, 0) + 1

    if mafia_votes:
        top = max(mafia_votes.values())
        leaders = sorted(t for t, votes in mafia_votes.items() if votes == top)
        victim = leaders[0] if len(leaders) == 1 else rng.choice(leaders)
        mafia_killers = sorted(
            a.actor_id
            for a in active_actions
            if a.action_type == ActionType.KILL.value
            and a.target_id == victim
            and (actor_role(a) and actor_role(a).team == Team.MAFIA)
        )
        try_kill(victim, "mafia", mafia_killers)

    # Маньяк: каждый маньяк действует самостоятельно
    for action in active_actions:
        role = actor_role(action)
        if (
            action.action_type == ActionType.KILL.value
            and role
            and role.night_action == ActionType.KILL
            and role.team == Team.NEUTRAL
        ):
            try_kill(action.target_id, "maniac", [action.actor_id])

    outcome.deaths = list(dead.values())

    # --- Стадия 4: CHECK -----------------------------------------------------
    for action in active_actions:
        role = actor_role(action)
        if (
            action.action_type == ActionType.CHECK.value
            and role
            and role.night_action == ActionType.CHECK
        ):
            target_role = _role_of(players, action.target_id)
            outcome.checks.append(
                CheckOutcome(
                    detective_id=action.actor_id,
                    target_id=action.target_id,
                    target_is_mafia=bool(target_role and target_role.team == Team.MAFIA),
                )
            )

    return outcome
