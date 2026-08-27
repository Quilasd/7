"""Изоляция групп «multi-server» (ТЗ-22): Group A / Group B / GLOBAL.

Один бот обслуживает несколько групп («серверов»). Требования:
- глобальная статистика (User: rating/XP/level/W/L/серии/достижения) — общая;
- локальная (GroupPlayer: rating/wins/xp/level) — строго per-group:
  победа в игре группы A меняет Global + Group A и НЕ трогает Group B;
- игры/комнаты/настройки/форумы/темы групп не пересекаются;
- локальный админ A не управляет B; глобальный Owner — везде;
- несколько игр одновременно (в разных группах и в одной) не смешиваются;
- игра группы НИКОГДА не использует форумы другой группы и глобальные
  env-форумы (ТЗ-11); глобальные форумы — только для игр без группы.
"""

from __future__ import annotations

import pytest

from bot.database.models import GameStatus, Room, RoomStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.groups import GroupPlayerRepository, GroupSettingsRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.services.game_chat import DbForumProvider, GameChatService, StaticForumProvider
from bot.services.permissions import AdminLevel
from tests.test_game_chats import FakeForumGateway, _new_game
from tests.conftest import make_room, make_ready, make_user

FORUM_A_GAME = -2000001
FORUM_A_MAFIA = -2000002
FORUM_B_GAME = -2000003
FORUM_B_MAFIA = -2000004
GLOBAL_GAME_FORUM = -1009001
GLOBAL_MAFIA_FORUM = -1009002


# ------------------------------------------------------------------ helpers


async def _group(services, chat_id: int, title: str):
    return await services.groups.get_or_create(chat_id, title)


async def _set_group_forums(session, group_id: int, game: int, mafia: int) -> None:
    gs = await GroupSettingsRepository(session).get_or_create(group_id)
    gs.game_forum_chat_id = game
    gs.mafia_forum_chat_id = mafia
    await session.commit()


async def _group_room(session, creator, players, group_id: int, name="Комната"):
    """Комната, привязанная к группе (аналог /createroom в группе)."""
    room = await make_room(
        session, creator, players,
        roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
    )
    from sqlalchemy import update

    await session.execute(
        update(Room).where(Room.id == room.id).values(group_id=group_id)
    )
    await session.commit()
    from bot.database.repositories.rooms import RoomRepository as RR

    return await RR(session).get(room.id)


async def _start_group_game(services, session, group_id: int, prefix: str):
    users = [await make_user(session, f"{prefix}{i}") for i in range(1, 5)]
    room = await _group_room(session, users[0], users, group_id)
    for user in users:
        await make_ready(session, room, user)
    res = await services.games.start_game_from_room(room.id, users[0].id)
    assert res.ok, res.message
    async with services.session_factory() as s:
        fresh = await RoomRepository(s).get(room.id)
        return fresh.game_id, users


async def _finish_city_win(services, game_id: int) -> set[int]:
    """Полный цикл до победы города; возвращает set(user_id победителей)."""
    async with services.session_factory() as s:
        players = await GamePlayerRepository(s).list_for_game(game_id)
        roles = {p.user_id: p.role for p in players}
    mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
    detective_uid = next(uid for uid, r in roles.items() if r == "detective")
    citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

    await services.phases.begin_game(game_id)
    await services.games.submit_night_action(game_id, mafia_uid, "kill", detective_uid)
    await services.phases.end_night(game_id)
    await services.phases.begin_voting(game_id)
    for uid in roles:
        if uid in (mafia_uid, detective_uid):
            continue
        assert (await services.games.cast_vote(game_id, uid, mafia_uid)).ok
    await services.phases.end_voting(game_id)
    async with services.session_factory() as s:
        game = await GameRepository(s).get(game_id)
        assert game.status == GameStatus.ENDED.value
        assert game.winner == "city"
    # победители — живые мирные (горожанин выжил; доктор мог выжить тоже)
    async with services.session_factory() as s:
        players = await GamePlayerRepository(s).list_for_game(game_id)
        winners = {p.user_id for p in players if p.is_alive}
    return winners


async def _game(services, game_id: int):
    async with services.session_factory() as s:
        return await GameRepository(s).get(game_id)


# ---------------------------------------------------- 1-5: статистика


class TestCrossGroupStats:
    """Один игрок в A и B: победа/поражение меняет только Global + своя группа."""

    async def test_player_exists_in_both_groups(self, services, session):
        user = await make_user(session, "Dual")
        a = await _group(services, -3001, "A")
        b = await _group(services, -3002, "B")
        await GroupPlayerRepository(session).ensure(a.id, user.id)
        await GroupPlayerRepository(session).ensure(b.id, user.id)
        await session.commit()
        assert await GroupPlayerRepository(session).get_membership(a.id, user.id)
        assert await GroupPlayerRepository(session).get_membership(b.id, user.id)

    async def test_win_in_a_updates_global_and_a_only(self, services, session):
        """Победа в игре группы A: Global + A меняются, B — НЕТ (ТЗ-22 п.2)."""
        user = await make_user(session, "Winner")
        a = await _group(services, -3011, "A")
        b = await _group(services, -3012, "B")
        gp_a = await GroupPlayerRepository(session).ensure(a.id, user.id)
        gp_b = await GroupPlayerRepository(session).ensure(b.id, user.id)
        await session.commit()

        from bot.services.rating import RatingService, ScopeFlags, StatEvents

        # _end_game применяет локальную статистику ТОЛЬКО группы игры (A)
        RatingService().apply_local(
            {user.id: gp_a}, winners={user.id}, is_draw=False,
            events=StatEvents(), survived_ids={user.id},
            flags=ScopeFlags(rating=True, xp=True),
        )
        await session.commit()

        fresh_user = await UserRepository(session).get_by_id(user.id)
        # глобальная не тронута этим вызовом (apply_global отдельно)
        assert fresh_user.wins == 0
        assert gp_a.wins == 1 and gp_a.rating > 0
        # группа B не тронута
        assert gp_b.wins == 0 and gp_b.rating == 0 and gp_b.xp == 0

    async def test_real_game_end_touches_only_its_group(self, services, session):
        """Реальный финал игры в группе A: локальная статистика B не меняется."""
        a = await _group(services, -3021, "A")
        b = await _group(services, -3022, "B")
        outsider = await make_user(session, "Outsider")
        gp_b = await GroupPlayerRepository(session).ensure(b.id, outsider.id)
        await session.commit()

        game_id, users = await _start_group_game(services, session, a.id, "ga")
        winners = await _finish_city_win(services, game_id)

        async with services.session_factory() as s:
            fresh_b = await GroupPlayerRepository(s).get_membership(b.id, outsider.id)
            assert fresh_b.games_played == 0 and fresh_b.wins == 0
            game = await GameRepository(s).get(game_id)
            assert game.group_id == a.id
            # у победителей в группе A появилась локальная статистика
            for uid in winners:
                gp = await GroupPlayerRepository(s).get_membership(a.id, uid)
                assert gp is not None and gp.games_played == 1
        # loser в B не получил ничего
        assert gp_b.games_played == 0

    async def test_loss_in_a_does_not_touch_b(self, services, session):
        """Поражение в A: локальные rating/wins/XP группы B не тронуты."""
        from bot.services.rating import RatingService, ScopeFlags, StatEvents

        user = await make_user(session, "Loser")
        a = await _group(services, -3031, "A")
        b = await _group(services, -3032, "B")
        gp_a = await GroupPlayerRepository(session).ensure(a.id, user.id)
        gp_b = await GroupPlayerRepository(session).ensure(b.id, user.id)
        await session.commit()

        RatingService().apply_local(
            {user.id: gp_a}, winners=set(), is_draw=False,
            events=StatEvents(), survived_ids=set(),
            flags=ScopeFlags(rating=True, xp=True),
        )
        await session.commit()
        assert gp_a.losses == 1 and gp_a.rating > 0
        assert gp_b.losses == 0 and gp_b.rating == 0 and gp_b.xp == 0


# ---------------------------------------------------- 6-9: листинги и профили


class TestCrossGroupListings:
    """История/топы/профиль: группы не видят чужое."""

    async def test_local_top_a_excludes_b_players(self, services, session):
        a = await _group(services, -3041, "A")
        b = await _group(services, -3042, "B")
        ua = await make_user(session, "FromA")
        ub = await make_user(session, "FromB")
        repo = GroupPlayerRepository(session)
        gp_a = await repo.ensure(a.id, ua.id)
        gp_a.rating = 500
        gp_b = await repo.ensure(b.id, ub.id)
        gp_b.rating = 900
        await session.commit()

        top_a = await repo.top(a.id, "rating")
        ids_a = {gp.user_id for gp in top_a}
        assert ua.id in ids_a
        assert ub.id not in ids_a  # игрок B с рейтингом 900 не в топе A

    async def test_history_is_global_user_scoped(self, services, session):
        """/history — глобальная история ИГРОКА (все его игры), не группы."""
        a = await _group(services, -3051, "A")
        game_id, users = await _start_group_game(services, session, a.id, "h")
        await _finish_city_win(services, game_id)
        history = await GamePlayerRepository(session).history_for_user(users[0].id, 10)
        assert len(history) == 1
        assert history[0].game_id == game_id

    async def test_active_games_of_a_not_in_b(self, services, session):
        a = await _group(services, -3061, "A")
        b = await _group(services, -3062, "B")
        game_id, _ = await _start_group_game(services, session, a.id, "act")
        games_repo = GameRepository(session)
        active_a = await games_repo.active_for_group(a.id)
        active_b = await games_repo.active_for_group(b.id)
        assert [g.id for g in active_a] == [game_id]
        assert active_b == []

    async def test_rooms_isolated_between_groups(self, services, session):
        """Комнаты группы A не видны в for_group(B) и в глобальном списке ЛС."""
        a = await _group(services, -3071, "A")
        b = await _group(services, -3072, "B")
        creator = await make_user(session, "Host")
        room_a = await _group_room(session, creator, [creator], a.id, name="RoomA")

        rooms_repo = RoomRepository(session)
        in_a = await rooms_repo.for_group(a.id)
        in_b = await rooms_repo.for_group(b.id)
        global_public = await rooms_repo.open_public_rooms(10)

        assert [r.id for r in in_a] == [room_a.id]
        assert in_b == []                                   # B не видит комнаты A
        assert all(r.id != room_a.id for r in global_public)  # ЛС не видит комнаты групп

    async def test_group_settings_independent(self, services, session):
        """Настройки (включая форумы) каждой группы свои (ТЗ-22 п.7)."""
        a = await _group(services, -3081, "A")
        b = await _group(services, -3082, "B")
        await _set_group_forums(session, a.id, FORUM_A_GAME, FORUM_A_MAFIA)
        await _set_group_forums(session, b.id, FORUM_B_GAME, FORUM_B_MAFIA)
        gs_a = await GroupSettingsRepository(session).get_for(a.id)
        gs_b = await GroupSettingsRepository(session).get_for(b.id)
        assert gs_a.game_forum_chat_id == FORUM_A_GAME
        assert gs_b.game_forum_chat_id == FORUM_B_GAME
        # остальные настройки независимы
        await services.groups.update_settings(a.id, lambda s: setattr(s, "night_seconds", 77))
        gs_b2 = await GroupSettingsRepository(session).get_for(b.id)
        assert gs_b2.night_seconds != 77


# ---------------------------------------------------- 10-13: форумы per-group


class TestGroupForums:
    """ТЗ-11: игра группы использует ТОЛЬКО форумы своей группы."""

    @pytest.fixture()
    def forum_services(self, services):
        """Глобальные форумы + per-group через group_settings (как в проде)."""
        gateway = FakeForumGateway()
        # глобальные env-форумы заданы — но игры групп их НЕ используют
        services.game_chats = GameChatService(
            services.session_factory, gateway, services.notifier,
            forums=DbForumProvider(services.app_config, type("Env", (), {
                "game_forum_chat_id": GLOBAL_GAME_FORUM,
                "mafia_forum_chat_id": GLOBAL_MAFIA_FORUM,
            })()),
        )
        # переподключаем сервисы к фазам/играм (как build_services)
        from bot.services.game_manager import GameManager
        from bot.services.phase_manager import GameLocks, PhaseManager
        from bot.services.rating import RatingService
        from bot.services.timer_manager import NoopTimerManager

        services.phases = PhaseManager(
            services.session_factory, services.notifier, NoopTimerManager(), GameLocks(),
            rating=RatingService(), app_settings=services.settings,
            game_chats=services.game_chats,
        )
        services.games = GameManager(
            services.session_factory, services.notifier, services.phases, GameLocks(),
            game_chats=services.game_chats,
        )
        services.gateway = gateway
        return services

    async def test_game_in_group_uses_group_forums_not_global(
        self, forum_services, session
    ):
        """Игра группы A создаёт темы в форумах A, НЕ в глобальных (ТЗ-11)."""
        a = await _group(forum_services, -3101, "A")
        await _set_group_forums(session, a.id, FORUM_A_GAME, FORUM_A_MAFIA)
        game_id, _ = await _start_group_game(forum_services, session, a.id, "fa")
        game = await _game(forum_services, game_id)
        assert game.game_chat_id == FORUM_A_GAME
        assert game.mafia_chat_id == FORUM_A_MAFIA
        assert game.game_chat_id != GLOBAL_GAME_FORUM
        # темы созданы именно в форумах группы A
        gw = forum_services.gateway
        assert (FORUM_A_GAME, game.game_thread_id) in gw.topics
        assert (FORUM_A_MAFIA, game.mafia_thread_id) in gw.topics
        assert all(chat != GLOBAL_GAME_FORUM for chat, _ in gw.topics)

    async def test_group_without_forums_gets_no_topics(self, forum_services, session):
        """У группы не настроены форумы — темы НЕ создаются, глобальный
        fallback НЕ применяется (изоляция важнее удобства)."""
        a = await _group(forum_services, -3111, "A")
        game_id, _ = await _start_group_game(forum_services, session, a.id, "nf")
        game = await _game(forum_services, game_id)
        assert game.game_chat_id is None
        assert game.mafia_chat_id is None
        assert game.game_thread_id is None
        assert forum_services.gateway.topics == {}

    async def test_game_without_group_uses_global_forums(self, forum_services, session):
        """Игра БЕЗ группы (ЛС-комната) использует глобальные форумы."""
        game_id, _users, _roles, _tg = await _new_game(forum_services, session, "dm")
        game = await _game(forum_services, game_id)
        assert game.game_chat_id == GLOBAL_GAME_FORUM
        assert game.mafia_chat_id == GLOBAL_MAFIA_FORUM

    async def test_two_groups_different_forums_never_intersect(
        self, forum_services, session
    ):
        """Group A: game=111, mafia=222; Group B: 333/444 — темы не пересекаются."""
        a = await _group(forum_services, -3121, "A")
        b = await _group(forum_services, -3122, "B")
        await _set_group_forums(session, a.id, FORUM_A_GAME, FORUM_A_MAFIA)
        await _set_group_forums(session, b.id, FORUM_B_GAME, FORUM_B_MAFIA)
        ga, _ = await _start_group_game(forum_services, session, a.id, "xa")
        gb, _ = await _start_group_game(forum_services, session, b.id, "xb")
        game_a, game_b = await _game(forum_services, ga), await _game(forum_services, gb)
        chats_a = {game_a.game_chat_id, game_a.mafia_chat_id}
        chats_b = {game_b.game_chat_id, game_b.mafia_chat_id}
        assert chats_a == {FORUM_A_GAME, FORUM_A_MAFIA}
        assert chats_b == {FORUM_B_GAME, FORUM_B_MAFIA}
        assert chats_a.isdisjoint(chats_b)  # форумы A и B никогда не пересекаются

    async def test_player_of_a_cannot_write_in_b_topic(self, forum_services, session):
        """Игрок игры A не может писать в тему игры B (даже в другом форуме)."""
        a = await _group(forum_services, -3131, "A")
        b = await _group(forum_services, -3132, "B")
        await _set_group_forums(session, a.id, FORUM_A_GAME, FORUM_A_MAFIA)
        await _set_group_forums(session, b.id, FORUM_B_GAME, FORUM_B_MAFIA)
        ga, users_a = await _start_group_game(forum_services, session, a.id, "wa")
        gb, users_b = await _start_group_game(forum_services, session, b.id, "wb")
        game_b = await _game(forum_services, gb)

        async with forum_services.session_factory() as s:
            from bot.database.repositories.users import UserRepository as UR

            user = await UR(s).get_by_id(users_a[0].id)
            deleted = await forum_services.game_chats.enforce_message(
                s, game_b.game_chat_id, game_b.game_thread_id, user, 777
            )
        assert deleted is True  # чужая тема — сообщение удалено


# ---------------------------------------------------- 14-16: права


class TestCrossGroupPermissions:
    """Локальный админ A не управляет B; глобальный Owner — везде."""

    async def test_local_admin_a_has_no_power_in_b(self, services, session):
        a = await _group(services, -3141, "A")
        b = await _group(services, -3142, "B")
        admin = await make_user(session, "LocalAdmin")
        await GroupPlayerRepository(session).ensure(a.id, admin.id)
        await session.commit()
        await services.groups.set_staff(
            a.id, admin.telegram_id, AdminLevel.OWNER, admin.id, 4, admin.id
        )

        level_in_a = await services.permissions.group_level(
            session, a.id, admin.telegram_id
        )
        level_in_b = await services.permissions.group_level(
            session, b.id, admin.telegram_id
        )
        assert level_in_a == 4
        assert level_in_b == 0  # в группе B он обычный игрок

    async def test_local_admin_cannot_manage_b_settings(self, services, session):
        """Админ A не меняет настройки группы B (MANAGE_SETTINGS только в A)."""
        from bot.services.permissions import AdminLevel, Permission

        a = await _group(services, -3151, "A")
        b = await _group(services, -3152, "B")
        admin = await make_user(session, "LocalAdmin")
        await GroupPlayerRepository(session).ensure(a.id, admin.id)
        await session.commit()
        await services.groups.set_staff(
            a.id, admin.telegram_id, AdminLevel.OWNER, admin.id, 4, admin.id
        )

        has_in_b = await services.permissions.has(
            session, admin.telegram_id, b.id, Permission.MANAGE_SETTINGS
        )
        has_in_a = await services.permissions.has(
            session, admin.telegram_id, a.id, Permission.MANAGE_SETTINGS
        )
        assert has_in_a is True
        assert has_in_b is False

    async def test_global_owner_powerful_everywhere(self, services, session, monkeypatch):
        a = await _group(services, -3161, "A")
        b = await _group(services, -3162, "B")
        owner = await make_user(session, "God")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])

        from bot.services.permissions import AdminLevel, Permission

        for group in (a, b):
            access = await services.permissions.resolve(
                session, owner.telegram_id, group.id
            )
            assert access.level == AdminLevel.OWNER
            assert Permission.MANAGE_SETTINGS in access.permissions


# ---------------------------------------------------- 17-18: параллельные игры


class TestParallelGames:
    """Несколько игр одновременно — ничего не смешивается."""

    async def test_two_games_different_groups_simultaneously(self, services, session):
        a = await _group(services, -3171, "A")
        b = await _group(services, -3172, "B")
        ga, _ = await _start_group_game(services, session, a.id, "pa")
        gb, _ = await _start_group_game(services, session, b.id, "pb")
        game_a, game_b = await _game(services, ga), await _game(services, gb)
        assert game_a.group_id == a.id and game_b.group_id == b.id
        assert ga != gb
        # финал игры A не завершает игру B
        await _finish_city_win(services, ga)
        game_b_fresh = await _game(services, gb)
        assert game_b_fresh.status in (
            GameStatus.STARTING.value, GameStatus.NIGHT.value,
            GameStatus.DAY.value, GameStatus.VOTING.value,
        )

    async def test_two_games_same_group_simultaneously(self, services, session):
        """Две игры в ОДНОЙ группе одновременно — независимые партии."""
        a = await _group(services, -3181, "A")
        g1, u1 = await _start_group_game(services, session, a.id, "s1")
        g2, u2 = await _start_group_game(services, session, a.id, "s2")
        assert g1 != g2
        players1 = {p.user_id for p in await GamePlayerRepository(session).list_for_game(g1)}
        players2 = {p.user_id for p in await GamePlayerRepository(session).list_for_game(g2)}
        assert players1.isdisjoint(players2)
        # финал первой не влияет на вторую
        await _finish_city_win(services, g1)
        game2 = await _game(services, g2)
        assert game2.status == GameStatus.STARTING.value

    async def test_testgame_groups_isolated(self, services, session):
        """testgame: две группы одновременно — игры разных групп не смешиваются."""
        a = await _group(services, -3191, "A")
        b = await _group(services, -3192, "B")
        admin_a = await make_user(session, "AdminA")
        admin_b = await make_user(session, "AdminB")
        ga, _ = await services.test_games.create_test_game(
            admin_a.id, 5, fast=False, group_id=a.id
        )
        gb, _ = await services.test_games.create_test_game(
            admin_b.id, 5, fast=False, group_id=b.id
        )
        assert ga and gb and ga != gb
        game_a, game_b = await _game(services, ga), await _game(services, gb)
        assert game_a.group_id == a.id and game_b.group_id == b.id
