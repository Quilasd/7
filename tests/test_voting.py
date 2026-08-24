"""Тесты голосования: подача, смена, большинство, ничьи, валидации."""

from __future__ import annotations


from bot.database.models import Game, GamePlayer, GameStatus
from bot.database.repositories.games import GamePlayerRepository
from bot.services.vote_manager import VoteManager
from tests.conftest import make_user


async def make_game(session, user_ids: list[int], alive: set[int] | None = None) -> Game:
    game = Game(
        status=GameStatus.VOTING.value,
        day_number=1,
        settings={"tie_rule": "revote", "vote_seconds": 30, "night_seconds": 60, "day_seconds": 60},
        vote_context={"round_no": 1, "candidates": None},
    )
    session.add(game)
    await session.flush()
    alive = alive if alive is not None else set(user_ids)
    for slot, uid in enumerate(user_ids, start=1):
        session.add(
            GamePlayer(
                game_id=game.id,
                user_id=uid,
                role="citizen" if uid != min(user_ids) else "mafia",
                status="ALIVE" if uid in alive else "DEAD",
                is_alive=uid in alive,
                slot=slot,
            )
        )
    await session.commit()
    return game


async def gp_of(session, game: Game, user_id: int) -> GamePlayer:
    return await GamePlayerRepository(session).get_by_user(game.id, user_id)


class TestCastAndTally:
    async def test_votes_tallied(self, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        vm = VoteManager(session)

        await vm.cast_vote(game, await gp_of(session, game, users[0].id), await gp_of(session, game, users[2].id))
        await vm.cast_vote(game, await gp_of(session, game, users[1].id), await gp_of(session, game, users[2].id))
        await vm.cast_vote(game, await gp_of(session, game, users[2].id), await gp_of(session, game, users[3].id))

        tally = await vm.votes.tally(game.id, 1, 1)
        assert tally == [(users[2].id, 2), (users[3].id, 1)]

    async def test_revote_updates_target(self, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        vm = VoteManager(session)

        voter = await gp_of(session, game, users[0].id)
        await vm.cast_vote(game, voter, await gp_of(session, game, users[1].id))
        await vm.cast_vote(game, voter, await gp_of(session, game, users[2].id))  # переголосовал

        tally = await vm.votes.tally(game.id, 1, 1)
        assert tally == [(users[2].id, 1)]
        votes = await vm.votes.round_votes(game.id, 1, 1)
        assert len(votes) == 1  # одна запись, а не две

    async def test_resolution_majority(self, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        vm = VoteManager(session)
        for voter_index, target_index in ((0, 2), (1, 2), (2, 3)):
            await vm.cast_vote(
                game,
                await gp_of(session, game, users[voter_index].id),
                await gp_of(session, game, users[target_index].id),
            )
        resolution = await vm.resolve(game)
        assert not resolution.is_tie
        assert resolution.lynched == users[2].id
        assert resolution.leader_votes == 2

    async def test_resolution_tie(self, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        vm = VoteManager(session)
        await vm.cast_vote(game, await gp_of(session, game, users[0].id), await gp_of(session, game, users[1].id))
        await vm.cast_vote(game, await gp_of(session, game, users[2].id), await gp_of(session, game, users[3].id))
        resolution = await vm.resolve(game)
        assert resolution.is_tie
        assert resolution.lynched is None
        assert set(resolution.tied_ids) == {users[1].id, users[3].id}


class TestValidation:
    async def test_dead_cannot_vote(self, session, services):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users], alive={u.id for u in users[:4]})
        result = await services.games.cast_vote(game.id, users[4].id, users[1].id)
        assert not result.ok

    async def test_cannot_vote_for_self(self, session, services):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        result = await services.games.cast_vote(game.id, users[0].id, users[0].id)
        assert not result.ok

    async def test_cannot_vote_for_dead(self, session, services):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users], alive={u.id for u in users[:4]})
        result = await services.games.cast_vote(game.id, users[0].id, users[4].id)
        assert not result.ok

    async def test_cannot_vote_outside_voting_phase(self, session, services):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        game.status = GameStatus.NIGHT.value
        await session.commit()
        result = await services.games.cast_vote(game.id, users[0].id, users[1].id)
        assert not result.ok

    async def test_revote_round_restricted_candidates(self, session, services):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game = await make_game(session, [u.id for u in users])
        game.vote_context = {"round_no": 2, "candidates": [users[1].id, users[3].id]}
        await session.commit()
        blocked = await services.games.cast_vote(game.id, users[0].id, users[2].id)
        assert not blocked.ok
        allowed = await services.games.cast_vote(game.id, users[0].id, users[1].id)
        assert allowed.ok
