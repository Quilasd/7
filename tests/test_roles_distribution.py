"""Тесты распределения ролей и валидации сетапа."""

from __future__ import annotations

import random

from bot.roles import get_role
from bot.services.role_manager import distribute_roles, validate_setup


def test_validate_setup_ok():
    assert validate_setup({"mafia": 2, "detective": 1, "doctor": 1}, 10, 4) == []


def test_validate_setup_errors():
    assert validate_setup({}, 10, 4)  # нет мафии
    assert validate_setup({"mafia": 0}, 10, 4)  # нет мафии
    assert validate_setup({"mafia": 10}, 10, 4)  # сумма = max, слота мирным нет
    assert validate_setup({"mafia": 11}, 10, 4)  # сумма > max
    assert validate_setup({"mafia": 1, "citizen": 3}, 10, 4)  # citizen вручную нельзя
    assert validate_setup({"mafia": 1, "unknown_role": 1}, 10, 4)  # неизвестная роль
    assert validate_setup({"mafia": 1}, 10, 25)  # min > max


def test_distribute_exact_counts():
    setup = {"mafia": 2, "detective": 1, "doctor": 1}
    users = list(range(1, 9))  # 8 игроков
    result = distribute_roles(users, setup, rng=random.Random(42))
    roles = list(result.roles_by_user.values())
    assert roles.count("mafia") == 2
    assert roles.count("detective") == 1
    assert roles.count("doctor") == 1
    assert roles.count("citizen") == 4  # остальные — мирные
    assert len(result.roles_by_user) == len(users)  # каждый получил ровно одну роль
    assert set(result.roles_by_user.keys()) == set(users)


def test_distribute_deterministic_with_seed():
    users = list(range(1, 21))
    setup = {"mafia": 3, "detective": 1, "doctor": 1, "maniac": 1}
    a = distribute_roles(users, setup, rng=random.Random(7))
    b = distribute_roles(users, setup, rng=random.Random(7))
    assert a.roles_by_user == b.roles_by_user


def test_distribute_all_valid_roles():
    setup = {"mafia": 1}
    users = list(range(1, 6))
    result = distribute_roles(users, setup, rng=random.Random(1))
    for role_id in result.roles_by_user.values():
        assert get_role(role_id) is not None
