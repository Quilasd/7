"""Тесты функции оценки достижений (чистая функция, без БД)."""

from __future__ import annotations

import pytest

from bot.services.achievements import GameAchievementContext, evaluate


def _ctx(**kw) -> GameAchievementContext:
    base = dict(
        roles={1: "mafia", 2: "detective", 3: "doctor", 4: "citizen", 5: "citizen", 6: "citizen"},
        winners={2, 3, 4, 5, 6},
        is_draw=False,
        survived_ids={2, 3, 4, 5, 6},
        kills={}, saves={}, correct_checks={}, correct_votes={},
        sacrificed_ids=set(), maniac_killers=set(),
        wins_before={i: 0 for i in range(1, 7)},
        win_streak_after={i: 0 for i in range(1, 7)},
    )
    base.update(kw)
    return GameAchievementContext(**base)


def test_city_win_and_first_win():
    earned = evaluate(_ctx(winners={2, 3, 4, 5, 6}))
    assert "city_win" in earned[2]
    assert "first_win" in earned[2]  # wins_before=0


def test_mafia_win():
    earned = evaluate(_ctx(winners={1}))
    assert "mafia_win" in earned[1]


def test_savior_for_heal_save():
    earned = evaluate(_ctx(saves={3: 1}, winners={2, 3, 4, 5, 6}))
    assert "savior" in earned[3]


def test_shield_for_bodyguard_sacrifice():
    earned = evaluate(_ctx(sacrificed_ids={3}, winners={2, 3, 4, 5, 6}))
    assert "shield" in earned[3]


def test_detective_accurate():
    earned = evaluate(_ctx(correct_checks={2: 1}, winners={2, 3, 4, 5, 6}))
    assert "detective_accurate" in earned[2]


def test_hunter_maniac_kill():
    earned = evaluate(_ctx(roles={1: "maniac", 2: "citizen"}, maniac_killers={1}, kills={1: 1}, winners={1}))
    assert "hunter" in earned[1]


def test_last_survivor_single_alive():
    earned = evaluate(_ctx(survived_ids={2}, winners={2}))
    assert "last_survivor" in earned[2]


def test_unstoppable_streak():
    earned = evaluate(_ctx(win_streak_after={2: 5}, winners={2, 3, 4, 5, 6}))
    assert "unstoppable" in earned[2]


def test_legendary_streak():
    earned = evaluate(_ctx(win_streak_after={2: 10}, winners={2, 3, 4, 5, 6}))
    assert "legendary_streak" in earned[2]


def test_no_achievements_on_draw():
    earned = evaluate(_ctx(is_draw=True, winners=set()))
    # при ничьей достижений за победу не выдаётся
    for uid, achs in earned.items():
        assert "city_win" not in achs
        assert "mafia_win" not in achs


def test_hidden_achievement_exists():
    from bot.services.achievements import all_achievements
    hidden = [a for a in all_achievements() if a.hidden]
    assert hidden  # есть скрытые достижения
