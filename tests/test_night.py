"""Тесты ночного резолвера: убийство, лечение, блокировка, проверка, жертва телохранителя."""

from __future__ import annotations

from bot.database.models import GameAction, GamePlayer
from bot.services.night_resolver import resolve_night


def gp(user_id: int, role: str, alive: bool = True) -> GamePlayer:
    return GamePlayer(
        game_id=1, user_id=user_id, role=role,
        status="ALIVE" if alive else "DEAD", is_alive=alive,
    )


def act(actor: int, target: int, action_type: str, day: int = 1) -> GameAction:
    return GameAction(
        game_id=1, actor_id=actor, target_id=target,
        action_type=action_type, phase="NIGHT", day_number=day,
    )


class TestKill:
    def test_mafia_kills_victim(self):
        players = [gp(1, "mafia"), gp(2, "mafia"), gp(3, "citizen"), gp(4, "citizen"), gp(5, "citizen")]
        actions = [act(1, 4, "kill"), act(2, 4, "kill")]
        outcome = resolve_night(actions, players)
        assert [d.user_id for d in outcome.deaths] == [4]
        assert outcome.deaths[0].cause == "mafia"
        assert set(outcome.deaths[0].killers) == {1, 2}

    def test_mafia_consensus_majority(self):
        players = [gp(1, "mafia"), gp(2, "mafia"), gp(3, "mafia"), gp(4, "citizen"), gp(5, "citizen")]
        # двое за игрока 4, один за 5 -> умирает 4
        actions = [act(1, 4, "kill"), act(2, 4, "kill"), act(3, 5, "kill")]
        outcome = resolve_night(actions, players)
        assert [d.user_id for d in outcome.deaths] == [4]

    def test_maniac_kills_independently(self):
        players = [gp(1, "maniac"), gp(2, "mafia"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 2, "kill"), act(2, 3, "kill")]  # маньяк убивает мафию!
        outcome = resolve_night(actions, players)
        deaths = {d.user_id: d.cause for d in outcome.deaths}
        assert deaths == {2: "maniac", 3: "mafia"}

    def test_double_attack_single_death(self):
        players = [gp(1, "mafia"), gp(2, "maniac"), gp(3, "citizen")]
        actions = [act(1, 3, "kill"), act(2, 3, "kill")]
        outcome = resolve_night(actions, players)
        assert [d.user_id for d in outcome.deaths] == [3]

    def test_dead_actors_action_ignored(self):
        players = [gp(1, "mafia"), gp(2, "citizen"), gp(3, "citizen")]
        players[0].is_alive = False  # мафия уже мертва (гонка)
        actions = [act(1, 2, "kill")]
        outcome = resolve_night(actions, players)
        assert outcome.deaths == []


class TestHeal:
    def test_doctor_saves_victim(self):
        players = [gp(1, "mafia"), gp(2, "doctor"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 3, "kill"), act(2, 3, "heal")]
        outcome = resolve_night(actions, players)
        assert outcome.deaths == []
        assert outcome.saves and outcome.saves[0].healer_id == 2

    def test_bodyguard_sacrifices_himself(self):
        players = [gp(1, "mafia"), gp(2, "bodyguard"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 3, "kill"), act(2, 3, "protect")]
        outcome = resolve_night(actions, players)
        assert [d.user_id for d in outcome.deaths] == [2]
        assert outcome.deaths[0].cause == "sacrifice"

    def test_doctor_does_not_sacrifice(self):
        players = [gp(1, "mafia"), gp(2, "doctor"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 3, "kill"), act(2, 3, "heal")]
        outcome = resolve_night(actions, players)
        assert all(d.user_id != 2 for d in outcome.deaths)


class TestBlock:
    def test_lover_blocks_doctor(self):
        players = [gp(1, "mafia"), gp(2, "doctor"), gp(3, "lover"), gp(4, "citizen"), gp(5, "citizen")]
        # любовница блокирует доктора -> лечение отменено -> жертва умирает
        actions = [act(1, 4, "kill"), act(2, 4, "heal"), act(3, 2, "block")]
        outcome = resolve_night(actions, players)
        assert [d.user_id for d in outcome.deaths] == [4]
        assert 2 in outcome.blocked_user_ids

    def test_lover_blocks_mafia_member(self):
        players = [gp(1, "mafia"), gp(2, "lover"), gp(3, "citizen"), gp(4, "citizen")]
        # единственная мафия заблокирована -> убийства нет
        actions = [act(1, 3, "kill"), act(2, 1, "block")]
        outcome = resolve_night(actions, players)
        assert outcome.deaths == []

    def test_blocked_detective_gets_no_result(self):
        players = [gp(1, "detective"), gp(2, "lover"), gp(3, "mafia"), gp(4, "citizen")]
        actions = [act(1, 3, "check"), act(2, 1, "block")]
        outcome = resolve_night(actions, players)
        assert outcome.checks == []


class TestCheck:
    def test_detective_finds_mafia(self):
        players = [gp(1, "detective"), gp(2, "mafia"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 2, "check")]
        outcome = resolve_night(actions, players)
        assert len(outcome.checks) == 1
        assert outcome.checks[0].target_is_mafia is True

    def test_detective_citizen_is_not_mafia(self):
        players = [gp(1, "detective"), gp(2, "citizen"), gp(3, "mafia"), gp(4, "citizen")]
        actions = [act(1, 2, "check")]
        outcome = resolve_night(actions, players)
        assert outcome.checks[0].target_is_mafia is False

    def test_detective_maniac_shows_not_mafia(self):
        # Документированное решение: маньяк для комиссии «не мафия»
        players = [gp(1, "detective"), gp(2, "maniac"), gp(3, "citizen"), gp(4, "citizen")]
        actions = [act(1, 2, "check")]
        outcome = resolve_night(actions, players)
        assert outcome.checks[0].target_is_mafia is False
