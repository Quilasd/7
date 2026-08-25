"""OWNER-only выдача рейтингов/XP/уровней/достижений + титулы.

Глобальная администрация (ADMIN_IDS = Senior Admin) НЕ может вручную менять
глобальную статистику — только владелец (OWNER_IDS). Проверка серверная.
"""

from __future__ import annotations

import pytest

import bot.handlers.admin as adm
import bot.handlers.profile as pf
from bot.services.progression import DEFAULT_PROGRESSION as prog
from tests.conftest import make_user
from tests.test_handlers_smoke import (
    FakeCommandObject,
    FakeMessage,
    FakeTgUser,
    call_like_aiogram,
)

pytestmark = pytest.mark.asyncio


async def _owner_setup(services, session, monkeypatch):
    """Патчим ИНСТАНС settings из services (PermissionService держит именно его)."""
    owner = await make_user(session, "Owner")
    target = await make_user(session, "Target")
    monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
    monkeypatch.setattr(services.settings, "_admins", [])
    return owner, target


async def _call(handler, *, user, text, command, session, services, db_user):
    msg = FakeMessage(FakeTgUser(user.telegram_id), text)
    await call_like_aiogram(
        handler, message=msg, command=command, session=session,
        services=services, db_user=db_user,
    )
    return msg


class TestOwnerStatsCommands:
    async def test_owner_set_and_add_rating(self, services, session, monkeypatch):
        owner, target = await _owner_setup(services, session, monkeypatch)
        msg = await _call(
            adm.cmd_owner_stats, user=owner, text="/set_rating",
            command=FakeCommandObject(f"{target.telegram_id} 1500", "set_rating"),
            session=session, services=services, db_user=owner,
        )
        assert target.rating == 1500
        assert any("1500" in t and "(#" in t for t in msg.answers)

        msg = await _call(
            adm.cmd_owner_stats, user=owner, text="/add_rating",
            command=FakeCommandObject(f"{target.telegram_id} -300", "add_rating"),
            session=session, services=services, db_user=owner,
        )
        assert target.rating == 1200

    async def test_owner_set_wins(self, services, session, monkeypatch):
        owner, target = await _owner_setup(services, session, monkeypatch)
        await _call(
            adm.cmd_owner_stats, user=owner, text="/set_wins",
            command=FakeCommandObject(f"{target.telegram_id} 42", "set_wins"),
            session=session, services=services, db_user=owner,
        )
        assert target.wins == 42

    async def test_owner_set_xp_recalculates_level(self, services, session, monkeypatch):
        owner, target = await _owner_setup(services, session, monkeypatch)
        await _call(
            adm.cmd_owner_stats, user=owner, text="/set_xp",
            command=FakeCommandObject(f"{target.telegram_id} 1000", "set_xp"),
            session=session, services=services, db_user=owner,
        )
        assert target.xp == 1000
        assert target.level == prog.level_for_xp(1000) == 6  # существующая система

    async def test_owner_set_level_syncs_xp(self, services, session, monkeypatch):
        owner, target = await _owner_setup(services, session, monkeypatch)
        await _call(
            adm.cmd_owner_stats, user=owner, text="/set_level",
            command=FakeCommandObject(f"{target.telegram_id} 10", "set_level"),
            session=session, services=services, db_user=owner,
        )
        assert target.level == 10
        assert target.xp == prog.threshold(10)          # XP синхронизирован
        assert prog.level_for_xp(target.xp) == 10       # и не «уехал»

    async def test_admin_cannot_change_stats(self, services, session, monkeypatch):
        """Senior Admin (ADMIN_IDS) не может менять рейтинг/XP/уровень."""
        admin = await make_user(session, "Senior")
        target = await make_user(session, "Target")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins", [admin.telegram_id])
        assert services.permissions.global_level(admin.telegram_id).value >= 4

        for name, args in (("set_rating", f"{target.telegram_id} 999"),
                           ("add_rating", f"{target.telegram_id} 1"),
                           ("set_wins", f"{target.telegram_id} 99"),
                           ("add_wins", f"{target.telegram_id} 1"),
                           ("set_xp", f"{target.telegram_id} 500"),
                           ("add_xp", f"{target.telegram_id} 5"),
                           ("set_level", f"{target.telegram_id} 50")):
            msg = await _call(
                adm.cmd_owner_stats, user=admin, text=f"/{name}",
                command=FakeCommandObject(args, name),
                session=session, services=services, db_user=admin,
            )
            assert any("Только владельцу" in t for t in msg.answers), name
        assert (target.rating, target.wins, target.xp, target.level) == (0, 0, 0, 1)

    async def test_profile_reflects_owner_changes(self, services, session, monkeypatch):
        """После выдачи профиль показывает новые значения и места (#N)."""
        owner, target = await _owner_setup(services, session, monkeypatch)
        other = await make_user(session, "Other")
        await _call(
            adm.cmd_owner_stats, user=owner, text="/set_rating",
            command=FakeCommandObject(f"{target.telegram_id} 1500", "set_rating"),
            session=session, services=services, db_user=owner,
        )
        msg = FakeMessage(FakeTgUser(target.telegram_id), "/profile")
        await pf.cmd_profile(msg, session=session, services=services, db_user=target, group=None)
        text = msg.answers[0]
        assert "⭐ Общий: <b>1500</b> <code>(#1)</code>" in text   # место пересчиталось
        assert "🌐 <b>ГЛОБАЛЬНО</b>" in text
        assert "В ЭТОЙ ГРУППЕ" not in text                        # вне группы — без локального


class TestOwnerAchievements:
    async def test_grant_and_remove_with_title(self, services, session, monkeypatch):
        from bot.database.repositories.social import (
            UserAchievementRepository,
            UserTitleRepository,
        )

        owner, target = await _owner_setup(services, session, monkeypatch)
        repo = UserAchievementRepository(session)

        # выдача: first_win открывает титул rookie
        msg = await _call(
            adm.cmd_owner_achievements, user=owner, text="/achievement_grant",
            command=FakeCommandObject(f"{target.telegram_id} first_win", "achievement_grant"),
            session=session, services=services, db_user=owner,
        )
        assert any("Первая кровь" in t for t in msg.answers)
        assert await repo.has(target.id, "first_win")
        titles = await UserTitleRepository(session).ids_of(target.id)
        assert "rookie" in titles

        # повторная выдача — уже есть
        msg = await _call(
            adm.cmd_owner_achievements, user=owner, text="/achievement_grant",
            command=FakeCommandObject(f"{target.telegram_id} first_win", "achievement_grant"),
            session=session, services=services, db_user=owner,
        )
        assert any("уже есть" in t for t in msg.answers)

        # снятие: достижение и титул (source=achievement) уходят, active_title чистится
        target.active_title = "rookie"
        await session.commit()
        msg = await _call(
            adm.cmd_owner_achievements, user=owner, text="/achievement_remove",
            command=FakeCommandObject(f"{target.telegram_id} first_win", "achievement_remove"),
            session=session, services=services, db_user=owner,
        )
        assert any("снят" in t for t in msg.answers)
        assert not await repo.has(target.id, "first_win")
        titles = await UserTitleRepository(session).ids_of(target.id)
        assert "rookie" not in titles
        assert target.active_title is None

    async def test_admin_cannot_touch_achievements(self, services, session, monkeypatch):
        admin = await make_user(session, "Senior")
        target = await make_user(session, "Target")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins", [admin.telegram_id])

        for name in ("achievement_grant", "achievement_remove"):
            msg = await _call(
                adm.cmd_owner_achievements, user=admin, text=f"/{name}",
                command=FakeCommandObject(f"{target.telegram_id} first_win", name),
                session=session, services=services, db_user=admin,
            )
            assert any("Только владельцу" in t for t in msg.answers), name
        from bot.database.repositories.social import UserAchievementRepository

        assert not await UserAchievementRepository(session).has(target.id, "first_win")

    async def test_unknown_achievement_rejected(self, services, session, monkeypatch):
        owner, target = await _owner_setup(services, session, monkeypatch)
        msg = await _call(
            adm.cmd_owner_achievements, user=owner, text="/achievement_grant",
            command=FakeCommandObject(f"{target.telegram_id} nope", "achievement_grant"),
            session=session, services=services, db_user=owner,
        )
        assert any("Неизвестное достижение" in t for t in msg.answers)


class TestLevelInfo:
    async def test_level_info_table(self, services, session):
        user = await make_user(session, "U")
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/level_info")
        await adm.cmd_level_info(msg)
        assert "ТАБЛИЦА УРОВНЕЙ" in msg.answers[0]
        assert f"Ур. 2 — {prog.threshold(2)} XP" in msg.answers[0]


class TestTitleAdminCommands:
    async def test_title_list_and_remove(self, services, session, monkeypatch):
        import bot.handlers.rewards as rw
        from bot.database.repositories.social import UserTitleRepository

        admin = await make_user(session, "Senior")
        target = await make_user(session, "T")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins", [admin.telegram_id])

        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/title_list")
        await call_like_aiogram(
            rw.cmd_title_list, message=msg, command=FakeCommandObject(None),
            session=session, services=services, db_user=admin,
        )
        assert any("КАТАЛОГ ТИТУЛОВ" in t and "Шерлок" in t for t in msg.answers)

        await UserTitleRepository(session).unlock(target.id, "rookie", source="admin")
        target.active_title = "rookie"
        await session.commit()

        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/title_remove")
        await call_like_aiogram(
            rw.cmd_title_remove, message=msg,
            command=FakeCommandObject(f"{target.telegram_id} rookie", "title_remove"),
            session=session, services=services, db_user=admin,
        )
        assert any("снят" in t for t in msg.answers)
        titles = await UserTitleRepository(session).ids_of(target.id)
        assert "rookie" not in titles and target.active_title is None
