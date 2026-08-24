"""Тесты условий победы (мафия / мирные / маньяк / ничья)."""

from __future__ import annotations


from bot.database.models import GamePlayer
from bot.services.role_manager import evaluate_win
from bot.database.models import WinningSide


def make_gp(user_id: int, role: str, alive: bool = True) -> GamePlayer:
    gp = GamePlayer(game_id=1, user_id=user_id, role=role, status="ALIVE" if alive else "DEAD", is_alive=alive)
    return gp


def test_mafia_wins_by_parity():
    players = [
        make_gp(1, "mafia", True),
        make_gp(2, "mafia", True),
        make_gp(3, "citizen", True),
        make_gp(4, "citizen", False),
    ]
    win = evaluate_win(players)
    assert win is not None and win.side == WinningSide.MAFIA
    assert set(win.winner_user_ids) == {1, 2}


def test_city_wins_when_mafia_dead():
    players = [
        make_gp(1, "mafia", False),
        make_gp(2, "citizen", True),
        make_gp(3, "detective", True),
        make_gp(4, "doctor", True),
    ]
    win = evaluate_win(players)
    assert win is not None and win.side == WinningSide.CITY
    assert set(win.winner_user_ids) == {2, 3, 4}


def test_maniac_wins_last_two():
    players = [
        make_gp(1, "maniac", True),
        make_gp(2, "citizen", True),
        make_gp(3, "mafia", False),
    ]
    win = evaluate_win(players)
    assert win is not None and win.side == WinningSide.MANIAC
    assert set(win.winner_user_ids) == {1}


def test_maniac_lone_survivor():
    players = [make_gp(1, "maniac", True)]
    win = evaluate_win(players)
    assert win.side == WinningSide.MANIAC


def test_mafia_priority_over_maniac():
    # 1 мафия + 1 маньяк: паритет мафии сильнее
    players = [
        make_gp(1, "mafia", True),
        make_gp(2, "maniac", True),
    ]
    win = evaluate_win(players)
    assert win.side == WinningSide.MAFIA


def test_no_win_ongoing():
    players = [
        make_gp(1, "mafia", True),
        make_gp(2, "citizen", True),
        make_gp(3, "citizen", True),
        make_gp(4, "detective", True),
    ]
    assert evaluate_win(players) is None


def test_draw_when_everybody_dead():
    players = [
        make_gp(1, "mafia", False),
        make_gp(2, "citizen", False),
    ]
    win = evaluate_win(players)
    assert win is not None and win.side == WinningSide.DRAW


def test_city_team_includes_special_roles():
    # Любовница и телохранитель — город
    players = [
        make_gp(1, "mafia", False),
        make_gp(2, "lover", True),
        make_gp(3, "bodyguard", True),
        make_gp(4, "maniac", False),
    ]
    win = evaluate_win(players)
    assert win.side == WinningSide.CITY
    assert set(win.winner_user_ids) == {2, 3}
