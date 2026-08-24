"""Группы: настройки (раздельные), локальная статистика и её ИЗОЛЯЦИЯ.

Требования спека:
- Group(telegram_chat_id, title) со своими настройками/админами/рейтингами;
- GroupSettings у каждой группы свои; игры в группе используют её настройки;
- локальная статистика (GroupPlayer) физически отделена от глобальной (User)
  и от локальной статистики ДРУГОЙ группы: изменение в A не трогает B.
"""

from __future__ import annotations

from bot.database.models import GroupPlayer, User
from bot.database.repositories.groups import GroupPlayerRepository
from bot.services.rating import RatingService, ScopeFlags, StatEvents
from tests.conftest import make_user


class TestGroupBasics:
    async def test_get_or_create_idempotent(self, services):
        g1 = await services.groups.get_or_create(-100111, "Клуб «Пик»")
        g2 = await services.groups.get_or_create(-100111, "Клуб «Пик»")
        assert g1.id == g2.id

    async def test_settings_defaults_and_persistence(self, services):
        group = await services.groups.get_or_create(-100200, "Тест")
        gs = await services.groups.get_settings(group.id)
        assert gs.min_players == 4 and gs.max_players == 10
        assert gs.night_seconds == 90 and gs.day_seconds == 180
        assert gs.rating_enabled is True and gs.xp_enabled is True
        assert gs.global_rating_enabled is True and gs.local_rating_enabled is True
        assert gs.debug_enabled is False
        assert gs.tie_rule == "revote"

        await services.groups.update_settings(
            group.id, lambda s: setattr(s, "night_seconds", 120)
        )
        fresh = await services.groups.get_settings(group.id)
        assert fresh.night_seconds == 120  # настройки хранятся в БД

    async def test_two_groups_different_settings(self, services):
        a = await services.groups.get_or_create(-100300, "A")
        b = await services.groups.get_or_create(-100400, "B")
        await services.groups.update_settings(a.id, lambda s: setattr(s, "max_players", 12))
        await services.groups.update_settings(
            b.id,
            lambda s: (setattr(s, "max_players", 8), setattr(s, "local_rating_enabled", False)),
        )
        sa = await services.groups.get_settings(a.id)
        sb = await services.groups.get_settings(b.id)
        assert sa.max_players == 12 and sa.local_rating_enabled is True
        assert sb.max_players == 8 and sb.local_rating_enabled is False


class TestLocalStatsIsolation:
    async def test_local_stats_do_not_touch_user_or_other_group(self, services, session):
        user = await make_user(session, "Solo")
        a = await services.groups.get_or_create(-100500, "A")
        b = await services.groups.get_or_create(-100600, "B")

        rating = RatingService()
        row_a = await GroupPlayerRepository(session).ensure(a.id, user.id)
        row_b = await GroupPlayerRepository(session).ensure(b.id, user.id)

        rating.apply_local(
            {user.id: row_a}, winners={user.id}, is_draw=False,
            events=StatEvents(kills={user.id: 1}), survived_ids={user.id},
            flags=ScopeFlags(rating=True, xp=True),
        )
        await session.commit()

        # изменилась ТОЛЬКО строка группы A
        assert row_a.rating == 115  # 100 победа + 5 убийство + 10 выживание
        assert row_a.wins == 1 and row_a.xp == 45 and row_a.level == 1  # 10+25+5+5
        # группа B не тронута
        assert row_b.rating == 0 and row_b.wins == 0 and row_b.xp == 0
        # глобальная статистика не тронута
        fresh_user = await session.get(User, user.id)
        assert fresh_user.rating == 0 and fresh_user.wins == 0 and fresh_user.xp == 0
        assert fresh_user.games_played == 0

    async def test_independent_wins_xp_level_per_group(self, services, session):
        user = await make_user(session, "Dual")
        a = await services.groups.get_or_create(-100700, "A")
        b = await services.groups.get_or_create(-100800, "B")

        # 3 победы в группе A, 1 поражение в группе B
        for _ in range(3):
            async with services.session_factory() as s:
                repo = GroupPlayerRepository(s)
                row = await repo.ensure(a.id, user.id)
                RatingService().apply_local(
                    {user.id: row}, winners={user.id}, is_draw=False,
                    events=StatEvents(), survived_ids={user.id},
                    flags=ScopeFlags(True, True),
                )
                await s.commit()
        async with services.session_factory() as s:
            repo = GroupPlayerRepository(s)
            row = await repo.ensure(b.id, user.id)
            RatingService().apply_local(
                {user.id: row}, winners=set(), is_draw=False,
                events=StatEvents(), survived_ids=set(),
                flags=ScopeFlags(True, True),
            )
            await s.commit()

        ga = await services.groups.local_player(a.id, user.id)
        gb = await services.groups.local_player(b.id, user.id)
        assert ga.games_played == 3 and ga.wins == 3 and ga.rating == 330
        assert ga.xp == 120 and ga.level == 2  # 3 * 40 = 120 -> уровень 2
        assert gb.games_played == 1 and gb.losses == 1 and gb.rating == 25
        assert gb.xp == 10 and gb.level == 1

    async def test_local_top_orders_by_rating(self, services, session):
        group = await services.groups.get_or_create(-100900, "Top")
        rating = RatingService()
        names = ("Ann", "Bob", "Cid")
        users = [await make_user(session, n) for n in names]
        # Ann: 2 победы (220), Bob: 1 победа (110), Cid: 1 поражение (25)
        plan = [(users[0], 2, True), (users[1], 1, True), (users[2], 1, False)]
        for user, games, won in plan:
            for i in range(games):
                async with services.session_factory() as s:
                    repo = GroupPlayerRepository(s)
                    row = await repo.ensure(group.id, user.id)
                    rating.apply_local(
                        {user.id: row},
                        winners={user.id} if won else set(),
                        is_draw=False,
                        events=StatEvents(),
                        survived_ids={user.id} if won else set(),
                        flags=ScopeFlags(True, True),
                    )
                    await s.commit()

        top = await services.groups.local_top(group.id)
        assert [gp.user_id for gp in top] == [users[0].id, users[1].id, users[2].id]
        assert top[0].rating == 220

    async def test_ensure_membership_idempotent(self, services, session):
        user = await make_user(session, "Once")
        group = await services.groups.get_or_create(-101000, "G")
        r1 = await GroupPlayerRepository(session).ensure(group.id, user.id)
        await session.commit()
        r2 = await GroupPlayerRepository(session).ensure(group.id, user.id)
        await session.commit()
        assert r1.id == r2.id
        assert isinstance(r2, GroupPlayer)


class TestGroupModerationData:
    async def test_warn_and_ban_are_local(self, services, session):
        target = await make_user(session, "Noisy")
        actor = await make_user(session, "Mod")
        a = await services.groups.get_or_create(-101100, "A")
        b = await services.groups.get_or_create(-101200, "B")

        assert await services.groups.warn(a.id, target.id, actor.id) == 1
        assert await services.groups.warn(a.id, target.id, actor.id) == 2
        assert await services.groups.warn(a.id, target.id, actor.id, delta=-1) == 1

        banned, _ = await services.groups.set_local_ban(a.id, target.id, True, actor.id)
        assert banned is True
        # локальный бан группы A не действует в B
        gb = await services.groups.local_player(b.id, target.id)
        assert gb is None  # в B даже участником не был
        ga = await services.groups.local_player(a.id, target.id)
        assert ga.is_banned is True and ga.warnings == 1
