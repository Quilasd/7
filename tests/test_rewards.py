"""Тесты наград: ивентовые награды, титулы, активный выбор, истечение срока."""

from __future__ import annotations

import pytest
from datetime import timedelta

from bot.database.models import User
from bot.services import titles as ttl
from bot.services import rewards as rw
from bot.database.repositories.social import UserTitleRepository
from bot.utils.helpers import utcnow
from tests.conftest import make_user


@pytest.mark.asyncio
class TestEventRewards:
    async def test_create_grant_list_activate(self, services, session):
        admin = await make_user(session, "Admin")
        player = await make_user(session, "Player")
        ok, _ = await services.rewards.create_reward(
            "halloween", "Хэллоуин 2025", "🎃", "За турнир", "event", None, admin.id
        )
        assert ok
        catalog = await services.rewards.list_catalog()
        assert any(r.code == "halloween" for r in catalog)

        ok, _ = await services.rewards.grant(player.id, "halloween", admin.id)
        assert ok
        # новая выдача сразу активна
        async with services.session_factory() as s:
            user = await __import__("bot.database.repositories.users", fromlist=["UserRepository"]).UserRepository(s).get_by_id(player.id)
            assert user.active_event_reward_id is not None
            display = await services.rewards.active_display(s, user)
            assert "Хэллоуин" in display

    async def test_duplicate_code_rejected(self, services, session):
        admin = await make_user(session, "Admin")
        await services.rewards.create_reward("tur", "Турнир", "🏅", "", "tournament", None, admin.id)
        ok, msg = await services.rewards.create_reward("tur", "Другой", "🏅", "", "tournament", None, admin.id)
        assert not ok
        assert "уже существует" in msg

    async def test_grant_unknown_code(self, services, session):
        admin = await make_user(session, "Admin")
        player = await make_user(session, "Player")
        ok, msg = await services.rewards.grant(player.id, "nope", admin.id)
        assert not ok

    async def test_expired_reward_not_active_display(self, services, session):
        admin = await make_user(session, "Admin")
        player = await make_user(session, "Player")
        await services.rewards.create_reward("tmp", "Временная", "⏳", "", "event", 7, admin.id)
        # выдаём с уже истёкшим сроком
        ok, _ = await services.rewards.grant(player.id, "tmp", admin.id, expires_days=0)
        assert ok
        async with services.session_factory() as s:
            from bot.database.repositories.users import UserRepository
            user = await UserRepository(s).get_by_id(player.id)
            # ручная простановка истёкшей даты для проверки active_display
            from bot.database.repositories.social import UserEventRewardRepository
            row = await UserEventRewardRepository(s).get(user.active_event_reward_id)
            row.expires_at = utcnow() - timedelta(days=1)
            await s.commit()
            display = await services.rewards.active_display(s, user)
            assert display == ""


@pytest.mark.asyncio
class TestTitles:
    async def test_admin_grant_and_set_active(self, services, session):
        admin = await make_user(session, "Admin")
        player = await make_user(session, "Player")
        ok = await rw.grant_title(session, player.id, "legend", source="admin")
        assert ok
        await session.commit()
        async with services.session_factory() as s:
            ids = await UserTitleRepository(s).ids_of(player.id)
            assert "legend" in ids
        ok = await rw.set_active_title(session, player.id, "legend")
        assert ok
        await session.commit()
        async with services.session_factory() as s:
            from bot.database.repositories.users import UserRepository
            user = await UserRepository(s).get_by_id(player.id)
            assert user.active_title == "legend"
            assert ttl.title_display(user.active_title) == "🏆 Легенда"

    async def test_set_active_requires_unlock(self, services, session):
        player = await make_user(session, "Player")
        ok = await rw.set_active_title(session, player.id, "legend")
        assert not ok  # титул ещё не открыт

    async def test_unknown_title_not_granted(self, services, session):
        player = await make_user(session, "Player")
        ok = await rw.grant_title(session, player.id, "nonexistent")
        assert not ok

    async def test_one_active_title(self, services, session):
        player = await make_user(session, "Player")
        await rw.grant_title(session, player.id, "legend", source="admin")
        await rw.grant_title(session, player.id, "rookie", source="admin")
        await session.commit()
        await rw.set_active_title(session, player.id, "rookie")
        await session.commit()
        async with services.session_factory() as s:
            from bot.database.repositories.users import UserRepository
            user = await UserRepository(s).get_by_id(player.id)
            # ровно один активный титул
            assert user.active_title == "rookie"
            assert ttl.title_display(user.active_title).startswith("🎯")
