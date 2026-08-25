"""OWNER-панель (/owner): доступ, разделы, FSM-потоки, подтверждения.

Ключевое: /owner и ВСЕ её callback-и доступны только OWNER_IDS — проверка
серверная в каждом handler (ADMIN/SENIOR ADMIN/Player получают отказ, даже
если сами отправят callback_data панели).
"""

from __future__ import annotations

import pytest

import bot.handlers.owner as ow
from bot.states import OwnerStates
from bot.utils.callbacks import OwnerCB
from tests.conftest import make_user
from tests.test_handlers_smoke import (
    FakeCallback,
    FakeMessage,
    FakeTgUser,
    call_like_aiogram,
)

pytestmark = pytest.mark.asyncio


class FakeFSM:
    """Минимальный дубль FSMContext для тестов панели."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


async def _setup_owner(services, session, monkeypatch, *, also_admin=False):
    owner = await make_user(session, "Owner")
    target = await make_user(session, "Target")
    admins = [owner.telegram_id] if also_admin else []
    monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
    monkeypatch.setattr(services.settings, "_admins", admins)
    return owner, target


def _cb(user, action: str, value: str = "") -> FakeCallback:
    return FakeCallback(FakeTgUser(user.telegram_id))


def _pack(action: str, value: str = "") -> OwnerCB:
    return OwnerCB(action=action, value=value)


class TestOwnerAccess:
    async def test_owner_can_open_panel(self, services, session, monkeypatch):
        owner, _ = await _setup_owner(services, session, monkeypatch)
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/owner")
        await ow.cmd_owner(msg, services=services, db_user=owner)
        assert any("ПАНЕЛЬ ВЛАДЕЛЬЦА" in t for t in msg.answers)

    @pytest.mark.parametrize("as_admin", [
        True,   # SENIOR ADMIN (ADMIN_IDS)
        False,  # обычный игрок
    ])
    async def test_non_owner_denied_command(self, services, session, monkeypatch, as_admin):
        someone = await make_user(session, "Stranger")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins",
                            [someone.telegram_id] if as_admin else [])
        msg = FakeMessage(FakeTgUser(someone.telegram_id), "/owner")
        await ow.cmd_owner(msg, services=services, db_user=someone)
        assert any("только владельцу" in t.lower() for t in msg.answers)

    @pytest.mark.parametrize("as_admin", [True, False])
    async def test_non_owner_denied_callbacks(self, services, session, monkeypatch, as_admin):
        """Подделанный callback_data панели не помогает: серверная проверка."""
        someone = await make_user(session, "Stranger")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins",
                            [someone.telegram_id] if as_admin else [])
        cb = FakeCallback(FakeTgUser(someone.telegram_id))
        # пробуем «опасные» callback-и напрямую
        await call_like_aiogram(
            ow.cb_main, callback=cb, callback_data=_pack("main"),
            session=session, services=services, state=FakeFSM(), db_user=someone,
        )
        assert any("Только владельцу" in a for a in cb.answers)
        cb2 = FakeCallback(FakeTgUser(someone.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb2, callback_data=_pack("confirm", "rating_set.1.99999"),
            session=session, services=services, state=FakeFSM(), db_user=someone,
        )
        assert any("Только владельцу" in a for a in cb2.answers)


class TestOwnerSections:
    """OWNER открывает все разделы панели — без падений, с контентом."""

    async def _open(self, handler, services, session, owner, action, value="", group=None,
                    state=None):
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            handler, callback=cb, callback_data=_pack(action, value),
            session=session, services=services, group=group,
            state=state or FakeFSM(), db_user=owner,
        )
        return cb

    async def test_all_sections_render(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        from bot.database.repositories.groups import GroupPlayerRepository

        group = await services.groups.get_or_create(-612000, "Клуб")
        await GroupPlayerRepository(session).ensure(group.id, target.id)
        target.rating, target.wins = 100, 5
        await session.commit()

        cases = [
            (ow.cb_main, "main", ""),
            (ow.cb_stats, "stats", ""),
            (ow.cb_players, "players", ""),
            (ow.cb_players_recent, "players_recent", ""),
            (ow.cb_player, "player", str(target.id)),
            (ow.cb_ratings, "ratings", "global.rating"),
            (ow.cb_ratings, "ratings", "global.wins"),
            (ow.cb_ratings, "ratings", "global.level"),
            (ow.cb_xp, "xp", ""),
            (ow.cb_leveltable, "leveltable", ""),
            (ow.cb_achievements, "achievements", ""),
            (ow.cb_titles, "titles", ""),
            (ow.cb_rewards, "rewards", ""),
            (ow.cb_testgame, "testgame", ""),
            (ow.cb_debug, "debug", ""),
            (ow.cb_system, "system", ""),
            (ow.cb_staff, "staff", ""),
        ]
        for handler, action, value in cases:
            cb = await self._open(handler, services, session, owner, action, value, group)
            # не упало (edit_or_answer без message — тихо; главное нет исключений)

        # игрок-карточка и рейтинги реально что-то содержат
        cb = await self._open(ow.cb_player, services, session, owner, "player", str(target.id))
        # cb_profile использует edit_or_answer с callback.message=None -> тихо;
        # проверяем через прямой вызов текстовой функции
        text, _kb = await ow._screen_player(session, services, target.id)
        assert "Общий рейтинг: <b>100</b>" in text
        assert "🏆 Победы: <b>5</b>" in text
        assert "🏅 Достижения: <b>0/12</b>" in text

        text, _kb = await ow._screen_ratings(session, "global", "rating")
        assert "ОБЩИЙ РЕЙТИНГ" in text

    async def test_level_table_matches_progression(self, services, session, monkeypatch):
        owner, _ = await _setup_owner(services, session, monkeypatch)
        from bot.services.progression import DEFAULT_PROGRESSION as prog

        text = ow._screen_leveltable()
        assert f"Уровень 2 → {prog.threshold(2)} XP" in text
        assert f"Уровень 10 → {prog.threshold(10)} XP" in text


class TestOwnerFlows:
    """Полные потоки: игрок → значение → подтверждение → применение."""

    async def test_rating_change_flow(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        monkeypatch.setattr(ow, "edit_or_answer", _fake_edit)

        # 1) кнопка «✏️ Установить общий» с выбранного экрана игрока
        state = FakeFSM()
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_act, callback=cb, callback_data=_pack("act", f"rating_set.{target.id}"),
            session=session, services=services, state=state, db_user=owner, group=None,
        )
        assert state.state == OwnerStates.value_input

        # 2) ввод числа
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "2000")
        await ow.process_value_input(msg, state=state, session=session, services=services)
        assert any("ИЗМЕНИТЬ" in t and "2000" in t for t in msg.answers)

        # 3) подтверждение (callback несёт всё закодированным)
        cb2 = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb2,
            callback_data=_pack("confirm", f"rating_set.{target.id}.2000"),
            session=session, services=services, state=state, db_user=owner,
        )
        from bot.database.repositories.users import UserRepository

        fresh = await UserRepository(session).get_by_id(target.id)
        assert fresh.rating == 2000
        # аудит записан
        from sqlalchemy import func, select

        from bot.database.models import AuditLog
        cnt = await session.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.action == "owner_rating_set")
        )
        assert int(cnt.scalar_one()) == 1

    async def test_xp_flow_updates_level(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        monkeypatch.setattr(ow, "edit_or_answer", _fake_edit)

        state = FakeFSM()
        msg = FakeMessage(FakeTgUser(owner.telegram_id), str(target.telegram_id))
        await state.update_data(action="xp_set")
        await state.set_state(OwnerStates.player_input)
        await ow.process_player_input(msg, state=state, session=session, services=services)
        assert state.state == OwnerStates.value_input

        msg = FakeMessage(FakeTgUser(owner.telegram_id), "1000")
        await ow.process_value_input(msg, state=state, session=session, services=services)
        assert any("1000" in t for t in msg.answers)

        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb,
            callback_data=_pack("confirm", f"xp_set.{target.id}.1000"),
            session=session, services=services, state=state, db_user=owner,
        )
        from bot.services.progression import DEFAULT_PROGRESSION as prog

        assert target.xp == 1000 and target.level == prog.level_for_xp(1000) == 6

    async def test_level_set_syncs_xp(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb,
            callback_data=_pack("confirm", f"level_set.{target.id}.10"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        from bot.services.progression import DEFAULT_PROGRESSION as prog

        assert target.level == 10 and target.xp == prog.threshold(10)

    async def test_achievement_grant_via_item(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        monkeypatch.setattr(ow, "edit_or_answer", _fake_edit)

        # экран достижений игрока → кнопка item ➕ first_win (ещё нет)
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_item, callback=cb,
            callback_data=_pack("item", f"ach.{target.id}.first_win"),
            session=session, services=services, db_user=owner,
        )
        # подтверждение
        cb2 = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb2,
            callback_data=_pack("confirm", f"ach.{target.id}.first_win.grant"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        from bot.database.repositories.social import (
            UserAchievementRepository,
            UserTitleRepository,
        )

        assert await UserAchievementRepository(session).has(target.id, "first_win")
        assert "rookie" in await UserTitleRepository(session).ids_of(target.id)  # титул открыт

        # снятие — и достижение, и титул
        cb3 = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb3,
            callback_data=_pack("confirm", f"ach.{target.id}.first_win.revoke"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        assert not await UserAchievementRepository(session).has(target.id, "first_win")
        assert "rookie" not in await UserTitleRepository(session).ids_of(target.id)

    async def test_title_grant_and_remove_via_item(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        for verb in ("grant", "remove"):
            cb = FakeCallback(FakeTgUser(owner.telegram_id))
            await call_like_aiogram(
                ow.cb_confirm, callback=cb,
                callback_data=_pack("confirm", f"title.{target.id}.sleuth.{verb}"),
                session=session, services=services, state=FakeFSM(), db_user=owner,
            )
        from bot.database.repositories.social import UserTitleRepository

        assert "sleuth" not in await UserTitleRepository(session).ids_of(target.id)

    async def test_reward_grant_and_revoke(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        ok, _msg = await services.rewards.create_reward(
            "newyear", "Новый год", "🎄", "праздник", "event", None, owner.id)
        await session.commit()
        assert ok

        # выдача через confirm
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb,
            callback_data=_pack("confirm", f"reward.{target.id}.grant.newyear"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        from bot.database.repositories.social import UserEventRewardRepository

        rows = await UserEventRewardRepository(session).of_user(target.id)
        assert len(rows) == 1 and rows[0].reward.code == "newyear"  # связь reward работает

        # забрать
        cb2 = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb2,
            callback_data=_pack("confirm", f"reward.{target.id}.{rows[0].id}"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        rows = await UserEventRewardRepository(session).of_user(target.id)
        assert rows == []

    async def test_maintenance_toggle(self, services, session, monkeypatch):
        owner, _ = await _setup_owner(services, session, monkeypatch)
        cb = FakeCallback(FakeTgUser(owner.telegram_id))
        await call_like_aiogram(
            ow.cb_confirm, callback=cb, callback_data=_pack("confirm", "maint.on"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        from bot.database.repositories.settings import AppSettingRepository

        stored = await AppSettingRepository(session).get_global()
        assert stored.get("maintenance") is True
        await call_like_aiogram(
            ow.cb_confirm, callback=cb, callback_data=_pack("confirm", "maint.off"),
            session=session, services=services, state=FakeFSM(), db_user=owner,
        )
        stored = await AppSettingRepository(session).get_global()
        assert stored.get("maintenance") is False

    async def test_find_player_flow(self, services, session, monkeypatch):
        owner, target = await _setup_owner(services, session, monkeypatch)
        state = FakeFSM()
        await state.update_data(action="find")
        await state.set_state(OwnerStates.player_input)
        msg = FakeMessage(FakeTgUser(owner.telegram_id), f"@{target.username}")
        await ow.process_player_input(msg, state=state, session=session, services=services)
        assert any("Общий рейтинг" in t for t in msg.answers)  # карточка игрока

    async def test_cancel_returns_to_menu(self, services, session, monkeypatch):
        owner, _ = await _setup_owner(services, session, monkeypatch)
        monkeypatch.setattr(ow, "edit_or_answer", _fake_edit)
        state = FakeFSM()
        await state.update_data(action="xp_add")
        await state.set_state(OwnerStates.player_input)
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/cancel")
        await ow.process_player_input(msg, state=state, session=session, services=services)
        assert state.state is None  # FSM завершена


class TestAdminIndependence:
    async def test_admin_panel_still_works(self, services, session, monkeypatch):
        """/admin не зависит от /owner."""
        import bot.handlers.admin as adm

        admin = await make_user(session, "Senior")
        monkeypatch.setattr(services.settings, "_owners", [])
        monkeypatch.setattr(services.settings, "_admins", [admin.telegram_id])
        from tests.conftest import SettingsStub

        monkeypatch.setattr("bot.handlers.admin.get_settings", lambda: services.settings)
        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/admin")
        await adm.cmd_admin(msg)
        assert any("АДМИН-ПАНЕЛЬ" in t for t in msg.answers)


async def _fake_edit(cb, text, kb=None):
    cb.answers.append(text)
