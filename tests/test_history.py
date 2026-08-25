"""Тесты истории игр (пагинация + детальный просмотр) и рангов профиля."""

from __future__ import annotations

import pytest

from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.users import UserRepository
from tests.conftest import make_ready, make_room, make_user


async def _finish_city_win(services, session, users):
    """Проводит партию до победы города. Возвращает game_id."""
    room = await make_room(
        session, users[0], users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
    )
    for u in users:
        await make_ready(session, room, u)
    await services.games.start_game_from_room(room.id, users[0].id)
    async with services.session_factory() as s:
        from bot.database.repositories.rooms import RoomRepository
        game_id = (await RoomRepository(s).get(room.id)).game_id
    players = await GamePlayerRepository(session).list_for_game(game_id)
    roles = {p.user_id: p.role for p in players}
    mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
    citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")
    await services.phases.begin_game(game_id)
    await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
    await services.phases.end_night(game_id)
    await services.phases.begin_voting(game_id)
    alive_city = [uid for uid, r in roles.items() if r != "mafia" and uid != citizen_uid]
    for uid in alive_city:
        await services.games.cast_vote(game_id, uid, mafia_uid)
    await services.phases.end_voting(game_id)
    return game_id


@pytest.mark.asyncio
class TestHistory:
    async def test_history_after_game_and_count(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _finish_city_win(services, session, users)

        repo = GamePlayerRepository(session)
        # игра завершилась
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
        rows = await repo.history_for_user(users[0].id, 10)
        assert len(rows) == 1
        assert rows[0].game_id == game_id
        assert await repo.history_count(users[0].id) == 1

    async def test_history_detail_has_players_and_winner(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _finish_city_win(services, session, users)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            players = await GamePlayerRepository(s).list_for_game(game_id)
        assert game.winner == "city"
        assert len(players) == 6  # состав сохранён
        # у завершённой игры есть started_at/ended_at для длительности
        assert game.started_at is not None and game.ended_at is not None

    async def test_history_empty_for_new_user(self, services, session):
        u = await make_user(session, "Newbie")
        assert await GamePlayerRepository(session).history_count(u.id) == 0
        assert await GamePlayerRepository(session).history_for_user(u.id, 10) == []


@pytest.mark.asyncio
class TestRanks:
    async def test_rank_methods_exclude_banned_test(self, services, session):
        from bot.database.models import User

        top = User(telegram_id=1, display_name="Top", rating=5000, wins=100, level=20, xp=9999)
        banned = User(telegram_id=2, display_name="Banned", rating=9999, wins=999, level=50,
                      xp=99999, is_banned=True)
        test = User(telegram_id=3, display_name="Test", rating=9999, wins=999, level=50,
                    xp=99999, is_test=True)
        session.add_all([top, banned, test])
        await session.commit()
        repo = UserRepository(session)
        # забаненные и тестовые исключены → top занимает 1 место
        assert await repo.rank_by_rating(5000) == 1
        assert await repo.rank_by_wins(100) == 1
        assert await repo.rank_by_level(20, 9999) == 1

    async def test_rank_orders_by_value(self, services, session):
        from bot.database.models import User
        a = User(telegram_id=10, display_name="A", rating=100)
        b = User(telegram_id=11, display_name="B", rating=300)
        c = User(telegram_id=12, display_name="C", rating=200)
        session.add_all([a, b, c])
        await session.commit()
        repo = UserRepository(session)
        # выше те, у кого больше: B(300)>C(200)>A(100)
        assert await repo.rank_by_rating(100) == 3
        assert await repo.rank_by_rating(300) == 1
        assert await repo.rank_by_rating(200) == 2

    async def test_profile_extras(self, services, session):
        from bot.handlers.profile import compute_profile_extras
        u = await make_user(session, "Hero")
        # дадим пользователю достижение + титул для отображения
        from bot.services import rewards as rw
        await rw.grant_title(session, u.id, "legend", source="admin")
        await rw.set_active_title(session, u.id, "legend")
        await session.commit()
        async with services.session_factory() as s:
            from bot.database.repositories.users import UserRepository
            user = await UserRepository(s).get_by_id(u.id)
            data = await compute_profile_extras(s, user, services)
        assert data["extras"]["title"].startswith("🏆")
        assert "achievements" in data["extras"]
        assert "ranks" in data and set(data["ranks"]) == {"rating", "wins", "level"}
