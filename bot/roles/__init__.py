"""Пакет ролей: импорт модулей регистрирует роли в реестре.

Добавление новой роли = один новый файл с register_role(...) и,
при необходимости, новая стадия в NIGHT_PIPELINE.
"""

from bot.roles.base import (
    NIGHT_PIPELINE,
    ActionType,
    Role,
    Team,
    all_roles,
    get_role,
    roles_registry,
    teammates_role_ids,
    team_of,
)

# Импорты ниже заполняют реестр (порядок = порядок в справочниках UI)
from bot.roles import mafia, citizen, detective, doctor, maniac, lover, bodyguard  # noqa: E402,F401

__all__ = [
    "NIGHT_PIPELINE",
    "ActionType",
    "Role",
    "Team",
    "all_roles",
    "get_role",
    "roles_registry",
    "teammates_role_ids",
    "team_of",
]
