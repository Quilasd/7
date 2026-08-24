"""Взаимодействие глобальной и локальной статистики через РЕАЛЬНЫЙ движок.

Полный игровой цикл в группе; GroupSettings группы управляет независимыми
переключателями: rating_enabled / global_rating_enabled / local_rating_enabled /
xp_enabled / global_xp_enabled / local_xp_enabled.

Требования спека: все 4 комбинации вкл/выкл рейтинга, независимость XP.
"""

from __future__ import annotations

from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository
from bot.database.repositories.groups import GroupPlayerRepository
from bot.database.repositories.users import UserRepository
from tests.conftest import make_room, make_ready, make_user


async def _play_city_win_in_group(services, session, group) -> dict:
    """Полный цикл до победы города; возвращает user_id участников."""
    users = [await make_user(session, f"G{i}") for i in range(1, 7)]
    room = await make_room(session, users[0], users)
    room.group_id = group.id
    await session.commit()
    for user in users:
        await make_ready(session, room, user)
    result = await services.games.start_game_from_room(room.id, users[0].id)
    assert result.ok, result.message

    async with services.session_factory() as s:
        from bot.database.repositories.rooms import RoomRepository

        game_id = (await RoomRepository(s).get(room.id)).game_id

    await services.phases.begin_game(game_id)
    async with services.session_factory() as s:
        players = await GamePlayerRepository(s).list_for_game(game_id)
        roles = {p.user_id: p.role for p in players}
    mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
    detective_uid = next(uid for uid, r in roles.items() if r == "detective")
    citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

    kill = await services.games.submit_night_action(game_id, mafia_uid, "kill", detective_uid)
    assert kill.ok
    await services.phases.end_night(game_id)
    await services.phases.begin_voting(game_id)

    for uid, role in roles.items():
        if uid == mafia_uid or uid == detective_uid:
            continue
        voted = await services.games.cast_vote(game_id, uid, mafia_uid)
        assert voted.ok
    await services.phases.end_voting(game_id)

    async with services.session_factory() as s:
        from bot.database.repositories.games import GameRepository

        game = await GameRepository(s).get(game_id)
        assert game.status == GameStatus.ENDED.value
        assert game.winner == "city"
    return {
        "game_id": game_id,
        "mafia_uid": mafia_uid,
        "citizen_uid": citizen_uid,
        "detective_uid": detective_uid,
    }


async def _stats(services, group_id, uid):
    async with services.session_factory() as s:
        user = await UserRepository(s).get_by_id(uid)
        gp = await GroupPlayerRepository(s).get_membership(group_id, uid)
    return user, gp


class TestGlobalLocalInterplay:
    async def test_default_all_on_global_and_local_both_applied(self, services, session):
        group = await services.groups.get_or_create(-300100, "A")
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        # победа 100 + выживание 10 + правильный голос 2 — одинаково в обоих скоупах
        assert user.rating == 112 and user.wins == 1 and user.xp == 42
        assert gp is not None and gp.rating == 112 and gp.wins == 1 and gp.xp == 42

        mafia_user, mafia_gp = await _stats(services, group.id, info["mafia_uid"])
        assert mafia_user.rating == 30 and mafia_gp.rating == 30  # 25 + убийство 5

    async def test_global_rating_off_local_on(self, services, session):
        group = await services.groups.get_or_create(-300200, "A")
        await services.groups.update_settings(
            group.id, lambda s: setattr(s, "global_rating_enabled", False)
        )
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        assert user.rating == 0        # глобальный рейтинг выключен
        assert user.wins == 1 and user.xp == 42  # победы и XP идут
        assert gp.rating == 112        # локальный рейтинг работает

    async def test_local_rating_off_global_on(self, services, session):
        group = await services.groups.get_or_create(-300300, "A")
        await services.groups.update_settings(
            group.id, lambda s: setattr(s, "local_rating_enabled", False)
        )
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        assert user.rating == 112
        assert gp.rating == 0          # локальный рейтинг выключен
        assert gp.wins == 1            # но игры/победы считаются

    async def test_master_rating_off_both_scopes(self, services, session):
        group = await services.groups.get_or_create(-300400, "A")
        await services.groups.update_settings(
            group.id, lambda s: setattr(s, "rating_enabled", False)
        )
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        assert user.rating == 0 and gp.rating == 0
        assert user.wins == 1 and gp.wins == 1   # победы считаются
        assert user.xp == 42 and gp.xp == 42     # XP не задет

    async def test_xp_toggles_independent_of_rating(self, services, session):
        group = await services.groups.get_or_create(-300500, "A")
        await services.groups.update_settings(
            group.id,
            lambda s: (setattr(s, "xp_enabled", False), setattr(s, "global_xp_enabled", False)),
        )
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        assert user.rating == 112 and gp.rating == 112
        assert user.xp == 0 and user.level == 1
        assert gp.xp == 0 and gp.level == 1

    async def test_only_global_xp_off(self, services, session):
        group = await services.groups.get_or_create(-300600, "A")
        await services.groups.update_settings(
            group.id, lambda s: setattr(s, "global_xp_enabled", False)
        )
        info = await _play_city_win_in_group(services, session, group)

        user, gp = await _stats(services, group.id, info["citizen_uid"])
        assert user.xp == 0 and user.level == 1
        assert gp.xp == 42

    async def test_game_in_group_does_not_affect_other_group(self, services, session):
        group_a = await services.groups.get_or_create(-300700, "A")
        group_b = await services.groups.get_or_create(-300800, "B")
        info = await _play_city_win_in_group(services, session, group_a)

        _, gp_b = await _stats(services, group_b.id, info["citizen_uid"])
        assert gp_b is None  # в группе B участника даже нет

    async def test_private_room_has_no_local_stats(self, services, session):
        """Игра вне группы (личная комната) — только глобальная статистика."""
        from bot.database.repositories.games import GameRepository
        from bot.database.repositories.rooms import RoomRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        room = await make_room(session, users[0], users)
        for user in users:
            await make_ready(session, room, user)
        assert (await services.games.start_game_from_room(room.id, users[0].id)).ok
        async with services.session_factory() as s:
            game_id = (await RoomRepository(s).get(room.id)).game_id
            assert (await GameRepository(s).get(game_id)).group_id is None
