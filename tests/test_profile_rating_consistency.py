"""Согласованность /profile и /rating: уровень/рейтинг/победы из ОДНОГО источника.

Баг (живой Telegram): /profile показывал «Уровень 1», а рейтинг — «Lv.2».
Причина: уровень имеет ДВА способа чтения —
  /profile      → level_for_xp(user.xp)  (вычисляется из XP, канонический источник);
  рейтинги/топы → сохранённая колонка User.level / GroupPlayer.level (кеш).
Write-пути кеш пересчитывают, но при смене кривой XP (c107da1: Lv.2 был 100 XP,
стал 150) существующие строки пересчитывает только alembic-миграция 0006 —
а бот, обновлённый через create_all (AUTO_CREATE_TABLES=true, без alembic),
её НЕ выполняет: кеш застревает на старой кривой и расходится с XP.

Фикс: sync_derived_levels() при старте бота приводит level = f(xp) в соответствие
с текущей кривой (идемпотентно, кривая — из ProgressionService, единственный
источник формулы). Инвариант: level ВСЕГДА равен level_for_xp(xp).
"""

from __future__ import annotations

from bot.database.database import sync_derived_levels
from bot.database.models import Group, GroupPlayer
from bot.database.repositories.groups import GroupPlayerRepository
from bot.database.repositories.users import UserRepository
from bot.handlers.profile import _full_profile_text
from bot.handlers.ratings import _global_page, _local_page_text
from bot.services.progression import DEFAULT_PROGRESSION as prog
from bot.services.rating import RatingService, ScopeFlags, StatEvents
from tests.conftest import make_user


async def _play_won_games(session, user, group_id: int, games: int, *, kills: int = 1) -> None:
    """«Игры» в группе — ровно то, что делает phase_manager._end_game:
    apply_global(User) + apply_local(GroupPlayer этой группы)."""
    repo = GroupPlayerRepository(session)
    for _ in range(games):
        row = await repo.ensure(group_id, user.id)
        events = StatEvents(kills={user.id: kills})
        RatingService().apply_local(
            {user.id: row}, winners={user.id}, is_draw=False,
            events=events, survived_ids={user.id},
            flags=ScopeFlags(rating=True, xp=True),
        )
        RatingService().apply_global(
            {user.id: user}, winners={user.id}, is_draw=False,
            events=events, survived_ids={user.id},
            flags=ScopeFlags(rating=True, xp=True),
        )
    await session.commit()


class TestRootCauseStaleLevelCache:
    """Репродукция бага + самовосстановление кеша уровней."""

    async def test_stale_level_diverges_then_startup_sync_heals(self, engine, services, session):
        """Строка со старой кривой (xp=120, level=2) даёт ровно баг пользователя:
        /profile — «Уровень 1» (из XP), рейтинг — «— 2» (устаревший кеш).
        После sync_derived_levels (выполняется при старте бота) — одно значение."""
        user = await make_user(session, "Stale")
        user.xp, user.level = 120, 2   # Lv.2 по СТАРОЙ кривой (порог был 100 XP)
        user.rating, user.wins = 150, 1
        group = Group(telegram_chat_id=-100900, title="mafia")
        session.add(group)
        await session.flush()
        session.add(GroupPlayer(group_id=group.id, user_id=user.id))
        await session.commit()
        uid, gid = user.id, group.id

        async with services.session_factory() as s:
            db_user = await UserRepository(s).get_by_id(uid)
            db_group = await s.get(Group, gid)
            profile = await _full_profile_text(s, services, db_user, db_group)
            rating_text, _ = await _global_page(s, "level", 0)
        # ДО фикса: расхождение — репродукция бага пользователя
        assert "📈 Уровень: <b>1</b>" in profile      # профиль: уровень из XP (120 < 150)
        assert "— 2" in rating_text                  # рейтинг: устаревший сохранённый level

        # старт бота: sync_derived_levels чинит кеш уровней
        assert await sync_derived_levels(engine) == 1

        async with services.session_factory() as s:
            db_user = await UserRepository(s).get_by_id(uid)
            assert db_user.level == prog.level_for_xp(db_user.xp) == 1
            db_group = await s.get(Group, gid)
            profile = await _full_profile_text(s, services, db_user, db_group)
            rating_text, _ = await _global_page(s, "level", 0)
        # ПОСЛЕ фикса: оба источника показывают одинаковый актуальный уровень
        assert "📈 Уровень: <b>1</b>" in profile
        assert "— 1" in rating_text and "— 2" not in rating_text

    async def test_sync_fixes_both_tables_and_is_idempotent(self, engine, services, session):
        user = await make_user(session, "U1")
        other = await make_user(session, "U2")
        group = Group(telegram_chat_id=-100910, title="G")
        session.add(group)
        await session.flush()
        session.add(GroupPlayer(group_id=group.id, user_id=user.id, xp=520, level=2))
        session.add(GroupPlayer(group_id=group.id, user_id=other.id, xp=0, level=1))
        user.xp, user.level = 1030, 3    # кеш отстал от новой кривой (надо 4)
        other.xp, other.level = 0, 1     # корректная строка — не должна тронуться
        await session.commit()

        assert await sync_derived_levels(engine) >= 1
        async with services.session_factory() as s:
            u1 = await UserRepository(s).get_by_id(user.id)
            u2 = await UserRepository(s).get_by_id(other.id)
            assert u1.level == prog.level_for_xp(1030) == 4
            assert u2.level == 1                          # корректные не тронуты
            row = await GroupPlayerRepository(s).get_membership(group.id, user.id)
            assert row.level == prog.level_for_xp(520) == 3
        # повторный запуск ничего не меняет (идемпотентность)
        assert await sync_derived_levels(engine) == 0


class TestProfileAndRatingAgree:
    """/profile и /rating показывают одинаковые данные из одной статистики."""

    async def test_level_2_visible_everywhere_after_real_wins(self, services, session):
        """ТЗ §7: Lv.1 -> сыграли -> Lv.2; /profile и /rating согласованы
        по уровню, рейтингу, победам и позициям."""
        user = await make_user(session, "Player")
        group = await services.groups.get_or_create(-100920, "mafia")

        # 4 победы в этой группе (xp 4*45=180 >= 150 -> Lv.2)
        await _play_won_games(session, user, group.id, 4)

        async with services.session_factory() as s:
            db_user = await UserRepository(s).get_by_id(user.id)
            db_group = await s.get(Group, group.id)
            profile = await _full_profile_text(s, services, db_user, db_group)
            players = await GroupPlayerRepository(s).top(group.id, "level")
            local_top, _ = _local_page_text(players, "level", 0, db_group)
            global_text, _ = await _global_page(s, "level", 0)

            # канонический уровень — из XP, и глобально и локально
            assert db_user.xp == 180 and prog.level_for_xp(db_user.xp) == 2
            assert db_user.level == prog.level_for_xp(db_user.xp)
        # /profile: глобальный блок И локальный блок — Lv.2
        assert "📈 Уровень: <b>2</b>" in profile
        assert "📈 Ур. <b>2</b>" in profile
        # /rating: локальный топ И глобальный топ — Lv.2
        assert "— 2" in local_top
        assert "— 2" in global_text
        # рейтинг/победы в профиле = актуальные значения (глобальные и локальные)
        assert f"⭐ Общий: <b>{db_user.rating}</b>" in profile
        assert f"🏆 Побед: <b>{db_user.wins}</b>" in profile
        # позиции: единственный игрок — #1 в глобальном и локальном блоках
        assert "📈 Уровень: <b>2</b> <code>(#1)</code>" in profile
        assert "📈 Ур. <b>2</b> <code>(#1)</code>" in profile

    async def test_positions_relative_to_same_group_players(self, services, session):
        """ТЗ §5: позиции (#N) в профиле считаются по ТОЙ ЖЕ группе и тому же
        набору игроков, что показывает локальный топ /rating."""
        me = await make_user(session, "Me")
        rival = await make_user(session, "Rival")
        group = await services.groups.get_or_create(-100930, "mafia")
        other_group = await services.groups.get_or_create(-100940, "other")

        # я: 1 победа; соперник в ЭТОЙ ЖЕ группе: 3 победы; у меня 5 побед в другой
        await _play_won_games(session, me, group.id, 1)
        await _play_won_games(session, rival, group.id, 3)
        await _play_won_games(session, me, other_group.id, 5)

        async with services.session_factory() as s:
            db_me = await UserRepository(s).get_by_id(me.id)
            db_group = await s.get(Group, group.id)
            profile = await _full_profile_text(s, services, db_me, db_group)
            players = await GroupPlayerRepository(s).top(group.id, "level")
            local_top, _ = _local_page_text(players, "level", 0, db_group)

        # в локальном топе группы соперник выше — и в профиле та же позиция
        assert local_top.index("Rival") < local_top.index("Me")
        assert "📈 Ур. <b>1</b> <code>(#2)</code>" in profile   # я #2 в ЭТОЙ группе
        # 5 побед в ДРУГОЙ группе не влияют на позиции в этой
        async with services.session_factory() as s:
            row = await GroupPlayerRepository(s).get_membership(group.id, me.id)
            assert await GroupPlayerRepository(s).rank_in_group(
                group.id, "level", row.level, row.xp) == 2


class TestMultiGroupLevelIsolation:
    """ТЗ §8: User X в группе A -> Lv.2, в группе B -> Lv.1; B не подхватывает Lv.2."""

    async def test_group_a_level2_group_b_level1(self, services, session):
        user = await make_user(session, "X")
        a = await services.groups.get_or_create(-100950, "A")
        b = await services.groups.get_or_create(-100960, "B")

        # 4 победы в группе A (локально Lv.2), 1 победа в группе B (45 XP -> Lv.1)
        await _play_won_games(session, user, a.id, 4)
        await _play_won_games(session, user, b.id, 1)

        async with services.session_factory() as s:
            db_user = await UserRepository(s).get_by_id(user.id)
            ga, gb = await s.get(Group, a.id), await s.get(Group, b.id)
            profile_a = await _full_profile_text(s, services, db_user, ga)
            profile_b = await _full_profile_text(s, services, db_user, gb)
            top_a = await GroupPlayerRepository(s).top(a.id, "level")
            top_b = await GroupPlayerRepository(s).top(b.id, "level")
            text_a, _ = _local_page_text(top_a, "level", 0, ga)
            text_b, _ = _local_page_text(top_b, "level", 0, gb)

        # локальный блок = уровень ИМЕННО в этой группе (chat_id + user_id)
        assert "📈 Ур. <b>2</b>" in profile_a       # A: Lv.2
        assert "📈 Ур. <b>1</b>" in profile_b       # B: Lv.1 — Lv.2 из A НЕ подтянулся
        # рейтинги согласованы с профилем: локальный топ показывает те же уровни
        assert "— 2" in text_a and "— 2" not in text_b
        assert "— 1" in text_b
        # глобальный блок одинаков в обеих группах (глобальный XP один на все группы)
        assert "📈 Уровень: <b>2</b>" in profile_a
        assert "📈 Уровень: <b>2</b>" in profile_b
