"""DEBUG MODE через реальный движок: флаги DEBUG_AFFECTS_*_STATS (4 комбинации),
fast-режим, роли/ночные действия/голосование, доступ по PermissionService
и уважение group_settings.debug_enabled (Owner переопределяет).

Тестовые игры идут через НАСТОЯЩИЙ PhaseManager — статистику применяет
тот же _end_game, что и для обычных игр.
"""

from __future__ import annotations

import asyncio

import bot.handlers.testgame as testgame_module
from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository
from bot.database.repositories.groups import GroupPlayerRepository
from bot.database.repositories.users import UserRepository
from bot.handlers.testgame import _debug_allowed, _parse_args
from bot.services.permissions import AdminLevel
from bot.database.repositories.groups import GroupAdminRepository
from tests.conftest import make_user


async def _run_test_game_to_end(services, admin, group_id=None) -> str:
    """Создаёт тестовую игру и гоняет её супервизором/скипами до конца."""
    game_id, _ = await services.test_games.create_test_game(
        admin.id, 5, group_id=group_id, supervisor_interval=0.05
    )
    assert game_id, "тестовая игра не создана"
    return await _drive(services, game_id)


async def _drive(services, game_id: int) -> str:
    from bot.database.repositories.games import GameRepository

    for _ in range(200):
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
        if game.status == GameStatus.ENDED.value:
            return game.winner
        if game.status == GameStatus.VOTING.value:
            for _ in range(60):
                await asyncio.sleep(0.05)
                async with services.session_factory() as s:
                    from bot.database.repositories.votes import VoteRepository

                    round_no = int((game.vote_context or {}).get("round_no", 1))
                    votes = await VoteRepository(s).round_votes(
                        game_id, game.day_number, round_no
                    )
                    players = await GamePlayerRepository(s).list_for_game(game_id)
                    alive_bots = [p for p in players if p.is_alive and p.user.is_test]
                if len(votes) >= len(alive_bots):
                    break
        await services.test_games.skip_phase(game_id)
        await asyncio.sleep(0.02)
    async with services.session_factory() as s:
        game = await GameRepository(s).get(game_id)
    assert game.status == GameStatus.ENDED.value, f"не завершилась: {game.status}"
    return game.winner


class TestDebugAffectsFlags:
    async def _admin_stats(self, services, admin_id, group_id=None):
        async with services.session_factory() as s:
            user = await UserRepository(s).get_by_id(admin_id)
            gp = (
                await GroupPlayerRepository(s).get_membership(group_id, admin_id)
                if group_id
                else None
            )
        return user, gp

    async def test_flags_off_off_no_stats(self, services, session):
        admin = await make_user(session, "Admin")
        group = await services.groups.get_or_create(-400100, "A")
        services.settings.debug_affects_global_stats = False
        services.settings.debug_affects_local_stats = False

        await _run_test_game_to_end(services, admin, group.id)

        user, gp = await self._admin_stats(services, admin.id, group.id)
        assert user.games_played == 0 and user.rating == 0 and user.xp == 0
        assert gp is None or gp.games_played == 0

    async def test_flags_global_only(self, services, session):
        admin = await make_user(session, "Admin")
        group = await services.groups.get_or_create(-400200, "A")
        services.settings.debug_affects_global_stats = True
        services.settings.debug_affects_local_stats = False

        await _run_test_game_to_end(services, admin, group.id)

        user, gp = await self._admin_stats(services, admin.id, group.id)
        assert user.games_played == 1 and user.rating >= 25
        assert gp is None or gp.games_played == 0

    async def test_flags_local_only(self, services, session):
        admin = await make_user(session, "Admin")
        group = await services.groups.get_or_create(-400300, "A")
        services.settings.debug_affects_global_stats = False
        services.settings.debug_affects_local_stats = True

        await _run_test_game_to_end(services, admin, group.id)

        user, gp = await self._admin_stats(services, admin.id, group.id)
        assert user.games_played == 0 and user.rating == 0
        assert gp is not None and gp.games_played == 1 and gp.rating >= 25

    async def test_flags_both(self, services, session):
        admin = await make_user(session, "Admin")
        group = await services.groups.get_or_create(-400400, "A")
        services.settings.debug_affects_global_stats = True
        services.settings.debug_affects_local_stats = True

        await _run_test_game_to_end(services, admin, group.id)

        user, gp = await self._admin_stats(services, admin.id, group.id)
        assert user.games_played == 1 and user.rating >= 25
        assert gp is not None and gp.games_played == 1 and gp.rating >= 25


class TestFastMode:
    async def test_fast_game_has_short_timers(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5, fast=True)
        assert game_id
        async with services.session_factory() as s:
            from bot.database.repositories.games import GameRepository

            game = await GameRepository(s).get(game_id)
        assert game.settings["night_seconds"] == 5
        assert game.settings["day_seconds"] == 5
        assert game.settings["vote_seconds"] == 5
        await services.test_games.finish(game_id)

    async def test_normal_game_timers_untouched(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5)
        async with services.session_factory() as s:
            from bot.database.repositories.games import GameRepository

            game = await GameRepository(s).get(game_id)
        assert game.settings["night_seconds"] != 5
        await services.test_games.finish(game_id)


class TestParseArgs:
    def test_parse(self):
        assert _parse_args(None) == (None, False)
        assert _parse_args("6") == (6, False)
        assert _parse_args("fast") == (None, True)
        assert _parse_args("6 fast") == (6, True)
        assert _parse_args("7 f") == (7, True)
        assert _parse_args("быстро") == (None, True)


class TestDebugPermissionGate:
    """_debug_allowed: Owner всегда; Admin+ с USE_DEBUG; группа — debug_enabled."""

    async def test_owner_overrides_group_debug_off(self, services, session, monkeypatch):
        owner = await make_user(session, "Owner")
        group = await services.groups.get_or_create(-400500, "A")
        services.settings._owners = [owner.telegram_id]
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)

        allowed, _why, level = await _debug_allowed(
            services, session, owner.telegram_id, group
        )
        assert allowed and level == AdminLevel.OWNER

    async def test_local_admin_blocked_when_group_debug_off(self, services, session, monkeypatch):
        admin = await make_user(session, "GroupAdmin")
        group = await services.groups.get_or_create(-400600, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, admin.id, 3, 0)
            await s.commit()
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)

        allowed, reason, _ = await _debug_allowed(services, session, admin.telegram_id, group)
        assert not allowed and "debug выключен" in reason

    async def test_local_admin_allowed_when_group_debug_on(self, services, session, monkeypatch):
        admin = await make_user(session, "GroupAdmin")
        group = await services.groups.get_or_create(-400700, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, admin.id, 3, 0)
            await s.commit()
        await services.groups.update_settings(group.id, lambda s: setattr(s, "debug_enabled", True))
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)

        allowed, _, level = await _debug_allowed(services, session, admin.telegram_id, group)
        assert allowed and level == AdminLevel.ADMIN

    async def test_moderator_lacks_use_debug(self, services, session, monkeypatch):
        mod = await make_user(session, "Mod")
        group = await services.groups.get_or_create(-400800, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, mod.id, 2, 0)
            await s.commit()
        await services.groups.update_settings(group.id, lambda s: setattr(s, "debug_enabled", True))
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)

        allowed, why, _ = await _debug_allowed(services, session, mod.telegram_id, group)
        assert not allowed and "USE_DEBUG" in why

    async def test_private_chat_uses_env_debug_mode(self, services, session, monkeypatch):
        admin = await make_user(session, "Env")
        services.settings._admins = [admin.telegram_id]  # senior admin глобально
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)

        services.settings.debug_mode = False
        allowed, reason, _ = await _debug_allowed(services, session, admin.telegram_id, None)
        assert not allowed and "DEBUG_MODE" in reason

        services.settings.debug_mode = True
        allowed, _, level = await _debug_allowed(services, session, admin.telegram_id, None)
        assert allowed and level == AdminLevel.SENIOR_ADMIN

    async def test_plain_player_denied(self, services, session, monkeypatch):
        player = await make_user(session, "Peasant")
        monkeypatch.setattr(testgame_module, "get_settings", lambda: services.settings)
        allowed, _, level = await _debug_allowed(services, session, player.telegram_id, None)
        assert not allowed and level == AdminLevel.PLAYER
