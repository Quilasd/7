"""Базовые примитивы системы ролей.

Как добавить новую роль (подробно — в README):
1. создать модуль в bot/roles/ и описать экземпляр Role;
2. зарегистрировать его через @register_role;
3. если роль требует особой логики ночи — расширить NIGHT_PIPELINE
   в bot/services/night_resolver.py (стадии упорядочены).

Role — неизменяемое описание. Поведенческие правила (например, «доктор не
лечит одного и того же два раза подряд») живут в сервисном слое и опираются
на флаги role, поэтому новые роли не требуют переписывания движка.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Team(str, enum.Enum):
    MAFIA = "mafia"
    CITY = "city"
    NEUTRAL = "neutral"


class ActionType(str, enum.Enum):
    KILL = "kill"
    HEAL = "heal"
    CHECK = "check"
    BLOCK = "block"
    PROTECT = "protect"


# Порядок обработки ночных действий: блокировки -> защиты -> убийства -> проверки.
# Расширяется вставкой новой стадии в нужную позицию.
NIGHT_PIPELINE: list[ActionType] = [
    ActionType.BLOCK,
    ActionType.PROTECT,
    ActionType.KILL,
    ActionType.CHECK,
]


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    emoji: str
    team: Team
    description: str
    night_action: ActionType | None = None
    action_prompt: str = ""            # «Кого убить?» и т.п.
    action_verb: str = ""              # «Устранить», «Вылечить»...
    can_target_self: bool = False
    knows_teammates: bool = False      # мафия знает своих
    # Особые правила, учитываемые движком:
    no_repeat_target: bool = False     # нельзя выбирать ту же цель две ночи подряд
    sacrifice_on_save: bool = False    # телохранитель гибнет, спасая цель
    win_condition_text: str = ""

    @property
    def title(self) -> str:
        return f"{self.emoji} {self.name}"

    def same_team(self, other: "Role") -> bool:
        return self.team == other.team


Registry = dict[str, Role]

_ROLE_REGISTRY: Registry = {}


def register_role(role: Role) -> Role:
    """Декоратор/функция регистрации роли в глобальном реестре."""
    if role.id in _ROLE_REGISTRY:
        raise ValueError(f"Дубликат роли: {role.id}")
    _ROLE_REGISTRY[role.id] = role
    return role


def get_role(role_id: str | None) -> Role | None:
    return _ROLE_REGISTRY.get(role_id or "")


def all_roles() -> list[Role]:
    return list(_ROLE_REGISTRY.values())


def roles_registry() -> Registry:
    return dict(_ROLE_REGISTRY)


def teammates_role_ids(role: Role) -> list[str]:
    """Роли, которые считаются «своими» (для списков союзников)."""
    return [r.id for r in all_roles() if r.id != role.id and r.team == role.team]


def team_of(role_id: str | None) -> Team:
    """Команда роли (для неизвестных — город, безопасный дефолт)."""
    role = get_role(role_id)
    return role.team if role else Team.CITY
