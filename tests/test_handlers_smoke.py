"""Smoke-тесты ХЕНДЛЕРОВ: вызовы команд с фейковыми объектами aiogram.

Сервисный уровень покрыт в остальных файлах; здесь проверяется, что
хендлеры groups_admin/testgame/ratings реально работают: не падают
NameError/AttributeError, отвечают ожидаемым текстом, уважают права.
"""

from __future__ import annotations

import bot.handlers.groups_admin as ga
import bot.handlers.testgame as tg
import bot.handlers.ratings as rt
from bot.database.repositories.groups import GroupAdminRepository
from bot.services.permissions import AdminLevel
from bot.utils.callbacks import SettingCB
from tests.conftest import make_user


# ------------------------------------------------------------------ фейки

class FakeTgUser:
    def __init__(self, telegram_id: int, username: str | None = None) -> None:
        self.id = telegram_id
        self.username = username
        self.first_name = "T"


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str = "supergroup") -> None:
        self.id = chat_id
        self.type = chat_type


class FakeBot:
    """ Telegram-бот: restrict_chat_member либо успех, либо TelegramAPIError."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def restrict_chat_member(self, **kwargs) -> bool:
        from aiogram.exceptions import TelegramAPIError

        self.calls.append(kwargs)
        if self.fail:
            raise TelegramAPIError(method="restrictChatMember", message="not enough rights")
        return True


class FakeMessage:
    def __init__(self, user: FakeTgUser, text: str, chat: FakeChat | None = None,
                 reply: "FakeMessage | None" = None, bot: FakeBot | None = None) -> None:
        self.from_user = user
        self.text = text
        self.chat = chat or FakeChat(user.id, "private")
        self.reply_to_message = reply
        self.bot = bot
        self.answers: list[str] = []
        self.keyboards: list = []

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:
        self.answers.append(text)
        self.keyboards.append(reply_markup)

    def get_args(self) -> str:
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


class FakeCommandObject:
    def __init__(self, args: str | None = None, command: str = "top") -> None:
        self.args = args
        self.command = command


class FakeCallback:
    def __init__(self, user: FakeTgUser) -> None:
        self.from_user = user
        self.message = None  # edit_or_answer тихо пропустит
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **kw) -> bool:
        self.answers.append(text or "")
        return True


class FakeState:
    def __init__(self) -> None:
        self.states: list = []

    async def set_state(self, state) -> None:
        self.states.append(state)


async def _set_staff(session_factory, group_id, user_id, level):
    async with session_factory() as s:
        await GroupAdminRepository(s).set_level(group_id, user_id, level, 0)
        await s.commit()


# ------------------------------------------------------------------ тесты

class TestProfileCommands:
    async def test_player_self_in_group(self, services, session):
        admin = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-600100, "A")
        await _set_staff(services.session_factory, group.id, admin.id, 4)

        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/player",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_player(msg, FakeCommandObject(None), session=session,
                            db_user=admin, group=group, services=services)
        assert any("ГЛОБАЛЬНО" in t for t in msg.answers)

    async def test_player_other_denied_without_permission(self, services, session):
        player = await make_user(session, "P")
        other = await make_user(session, "Other")
        group = await services.groups.get_or_create(-600200, "A")

        msg = FakeMessage(FakeTgUser(player.telegram_id), f"/player {other.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_player(msg, FakeCommandObject(str(other.telegram_id)), session=session,
                            db_user=player, group=group, services=services)
        assert any("VIEW_PROFILE" in t for t in msg.answers)

    async def test_player_other_with_helper(self, services, session):
        helper = await make_user(session, "Helper")
        other = await make_user(session, "Other")
        group = await services.groups.get_or_create(-600300, "A")
        await _set_staff(services.session_factory, group.id, helper.id, 1)

        msg = FakeMessage(FakeTgUser(helper.telegram_id), f"/player @{other.username}",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_player(msg, FakeCommandObject(f"@{other.username}"), session=session,
                            db_user=helper, group=group, services=services)
        assert len(msg.answers) >= 1 and "VIEW_PROFILE" not in msg.answers[0]

    async def test_player_unknown_target(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-600400, "A")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/player 999",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_player(msg, FakeCommandObject("999"), session=session,
                            db_user=player, group=group, services=services)
        assert any("не найден" in t for t in msg.answers)


class TestModerationCommands:
    async def test_warnings_requires_target(self, services, session):
        mod = await make_user(session, "Mod")
        group = await services.groups.get_or_create(-600500, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        msg = FakeMessage(FakeTgUser(mod.telegram_id), "/warnings",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("Укажи игрока" in t for t in msg.answers)

    async def test_warn_via_reply(self, services, session):
        mod = await make_user(session, "Mod")
        noisy = await make_user(session, "Noisy")
        group = await services.groups.get_or_create(-600600, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        reply = FakeMessage(FakeTgUser(noisy.telegram_id), "спам")
        msg = FakeMessage(FakeTgUser(mod.telegram_id), "/warn",
                          chat=FakeChat(group.telegram_chat_id), reply=reply)
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("выдано предупреждение" in t for t in msg.answers)
        gp = await services.groups.local_player(group.id, noisy.id)
        assert gp is not None and gp.warnings == 1

    async def test_warn_denied_for_player(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-600700, "A")
        reply = FakeMessage(FakeTgUser(player.telegram_id + 1), "x")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/warn",
                          chat=FakeChat(group.telegram_chat_id), reply=reply)
        await ga.cmd_warns(msg, session=session, group=group, db_user=player, services=services)
        assert any("WARN_PLAYER" in t for t in msg.answers)

    async def test_mute_with_bot_rights_and_without(self, services, session):
        mod = await make_user(session, "Mod")
        noisy = await make_user(session, "Noisy2")
        group = await services.groups.get_or_create(-600800, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        reply = FakeMessage(FakeTgUser(noisy.telegram_id), "спам")
        for fail, expected in ((False, "мут на 60"), (True, "нужны права админа")):
            msg = FakeMessage(FakeTgUser(mod.telegram_id), "/mute",
                              chat=FakeChat(group.telegram_chat_id), reply=reply,
                              bot=FakeBot(fail=fail))
            await ga.cmd_mute(msg, session=session, group=group, db_user=mod, services=services)
            assert any(expected in t for t in msg.answers), (fail, msg.answers)


class TestSettingsCommand:
    async def test_settings_menu_requires_rights(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-600900, "A")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/settings",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_settings(msg, session=session, group=group, services=services)
        assert any("MANAGE_SETTINGS" in t for t in msg.answers)

    async def test_settings_menu_senior(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601000, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/settings",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_settings(msg, session=session, group=group, services=services)
        assert any("НАСТРОЙКИ ГРУППЫ" in t for t in msg.answers)
        assert msg.keyboards[0] is not None

    async def test_settings_private_chat_rejected(self, services, session):
        boss = await make_user(session, "Boss")
        services.settings._owners = [boss.telegram_id]
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/settings")
        await ga.cmd_settings(msg, session=session, group=None, services=services)
        assert any("только внутри группы" in t for t in msg.answers)


class TestSettingsCallbacks:
    async def _cb(self, services, session, boss, group, action, value=""):
        cb = FakeCallback(FakeTgUser(boss.telegram_id))
        await ga.cb_settings(cb, SettingCB(action=action, value=value), session=session,
                             group=group, services=services, db_user=boss)
        return cb

    async def test_toggle_global_rating(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601100, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)

        await self._cb(services, session, boss, group, "set", "grating")
        gs = await services.groups.get_settings(group.id)
        assert gs.global_rating_enabled is False

        await self._cb(services, session, boss, group, "set", "grating")
        gs = await services.groups.get_settings(group.id)
        assert gs.global_rating_enabled is True

    async def test_number_plus30_night(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601200, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)

        await self._cb(services, session, boss, group, "set", "night-plus30")
        gs = await services.groups.get_settings(group.id)
        assert gs.night_seconds == 120  # 90 дефолт + 30

    async def test_sections_render(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601300, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        for section in ("menu", "players", "timers", "roles", "voting", "progression", "extra"):
            cb = await self._cb(services, session, boss, group, section)
            assert cb.answers, section  # answer() вызван без исключений

    async def test_denied_without_rights(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-601400, "A")
        cb = FakeCallback(FakeTgUser(player.telegram_id))
        await ga.cb_settings(cb, SettingCB(action="set", value="grating"), session=session,
                             group=group, services=services, db_user=player)
        assert any("MANAGE_SETTINGS" in t for t in cb.answers)


class TestStaffCommands:
    async def test_staff_add_and_promote(self, services, session):
        boss = await make_user(session, "Chief")
        rookie = await make_user(session, "Rookie")
        group = await services.groups.get_or_create(-601500, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)

        msg = FakeMessage(FakeTgUser(boss.telegram_id), f"/staff_add @{rookie.username} 3",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff_add(msg, FakeCommandObject(f"@{rookie.username} 3"), session=session,
                               group=group, services=services, db_user=boss)
        assert any("✅" in t for t in msg.answers)
        assert await services.permissions.group_level(
            session, group.id, rookie.telegram_id) == AdminLevel.ADMIN

    async def test_staff_add_cannot_grant_equal_level(self, services, session):
        boss = await make_user(session, "Chief")
        rookie = await make_user(session, "Rookie2")
        group = await services.groups.get_or_create(-601600, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)

        msg = FakeMessage(FakeTgUser(boss.telegram_id), f"/staff_add @{rookie.username} 4",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff_add(msg, FakeCommandObject(f"@{rookie.username} 4"), session=session,
                               group=group, services=services, db_user=boss)
        assert any("равный" in t or "⛔️" in t for t in msg.answers)

    async def test_staff_listing(self, services, session):
        boss = await make_user(session, "Chief")
        group = await services.groups.get_or_create(-601700, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/staff",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff(msg, session=session, group=group, services=services)
        assert any("Senior" in t or "штаб пуст" in t for t in msg.answers)


class TestSystemCommands:
    async def test_debug_help_owner_only(self, services, session, monkeypatch):
        owner = await make_user(session, "Owner")
        senior = await make_user(session, "Senior")
        services.settings._owners = [owner.telegram_id]
        services.settings._admins = [senior.telegram_id]
        # get_settings импортируется внутри хендлера — патчим модуль-источник
        monkeypatch.setattr("bot.config.get_settings", lambda: services.settings)

        # владелец получает справочник с диагностикой
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/debug_help")
        await ga.cmd_debug_help(msg, session=session, services=services, group=None)
        assert any("СПРАВОЧНИК ВЛАДЕЛЬЦА" in t for t in msg.answers)
        assert any("ты владелец ✅" in t for t in msg.answers)

        # senior admin (4) — отказ, справочника нет
        msg2 = FakeMessage(FakeTgUser(senior.telegram_id), "/debug_help")
        await ga.cmd_debug_help(msg2, session=session, services=services, group=None)
        assert any("только глобальному Owner" in t for t in msg2.answers)
        assert not any("СПРАВОЧНИК ВЛАДЕЛЬЦА" in t for t in msg2.answers)

    async def test_admin_panel_accepts_owner_without_admin_ids(self, monkeypatch):
        """Владелец из OWNER_IDS должен попадать в /admin даже вне ADMIN_IDS."""
        import bot.handlers.admin as admin_mod

        class EnvStub:
            def is_admin(self, telegram_id: int) -> bool:
                return False  # ADMIN_IDS пуст — раньше владелец получал отказ

            def is_owner(self, telegram_id: int) -> bool:
                return telegram_id == 424242

            debug_mode = True

        monkeypatch.setattr(admin_mod, "get_settings", lambda: EnvStub())
        assert admin_mod._is_admin(424242) is True   # владелец проходит
        assert admin_mod._is_admin(111111) is False  # посторонний — нет

    async def test_botstats(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601800, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/botstats",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_botstats(msg, session=session, services=services, group=group)
        assert any("СТАТИСТИКА БОТА" in t for t in msg.answers)

    async def test_broadcast_sets_state(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-601900, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/broadcast",
                          chat=FakeChat(group.telegram_chat_id))
        state = FakeState()
        await ga.cmd_broadcast(msg, session=session, group=group, services=services, state=state)
        assert any("рассылк" in t.lower() for t in msg.answers)
        assert state.states  # set_state вызван

    async def test_broadcast_denied_for_admin_level3(self, services, session):
        admin = await make_user(session, "Admin3")
        group = await services.groups.get_or_create(-602000, "A")
        await _set_staff(services.session_factory, group.id, admin.id, 3)
        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/broadcast",
                          chat=FakeChat(group.telegram_chat_id))
        state = FakeState()
        await ga.cmd_broadcast(msg, session=session, group=group, services=services, state=state)
        assert any("BROADCAST" in t for t in msg.answers)
        assert not state.states

    async def test_maintenance_toggle(self, services, session):
        owner = await make_user(session, "Owner")
        services.settings._owners = [owner.telegram_id]
        group = await services.groups.get_or_create(-602100, "A")
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/maintenance",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_maintenance(msg, session=session, services=services, group=group)
        assert any("ВКЛ" in t for t in msg.answers)

        # второе переключение — обратно ВЫКЛ
        msg2 = FakeMessage(FakeTgUser(owner.telegram_id), "/maintenance",
                           chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_maintenance(msg2, session=session, services=services, group=group)
        assert any("ВЫКЛ" in t for t in msg2.answers)


class TestGameCommands:
    async def test_games_empty_list(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-602200, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/games",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_games(msg, session=session, group=group, services=services)
        assert msg.answers  # что-то ответило и не упало

    async def test_rooms_listing(self, services, session):
        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-602300, "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/rooms",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_rooms(msg, FakeCommandObject(""), session=session,
                           group=group, services=services)
        assert msg.answers


class TestTestgameHandler:
    async def test_testgame_introduces_and_creates(self, services, session, monkeypatch):
        admin = await make_user(session, "Admin")
        services.settings._admins = [admin.telegram_id]
        services.settings.debug_mode = True
        monkeypatch.setattr(tg, "get_settings", lambda: services.settings)

        msg = FakeMessage(FakeTgUser(admin.telegram_id), "/testgame")
        await tg.cmd_testgame(msg, command=FakeCommandObject(None), session=session,
                              group=None, services=services, db_user=admin)
        assert any("ТЕСТОВЫЙ РЕЖИМ" in t for t in msg.answers)

        msg2 = FakeMessage(FakeTgUser(admin.telegram_id), "/testgame 5")
        await tg.cmd_testgame(msg2, command=FakeCommandObject("5"), session=session,
                              group=None, services=services, db_user=admin)
        assert any("создана" in t.lower() for t in msg2.answers)
        assert msg2.keyboards[-1] is not None  # пульт управления

        services.test_games.stop_all()

    async def test_testgame_denied_for_player(self, services, session, monkeypatch):
        player = await make_user(session, "Peasant")
        services.settings.debug_mode = True
        monkeypatch.setattr(tg, "get_settings", lambda: services.settings)
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/testgame")
        await tg.cmd_testgame(msg, command=FakeCommandObject(None), session=session,
                              group=None, services=services, db_user=player)
        assert any("USE_DEBUG" in t for t in msg.answers)


class TestRatingsHandlers:
    async def test_top_in_group_defaults_local(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-602400, "A")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/top",
                          chat=FakeChat(group.telegram_chat_id))
        await rt.cmd_top(msg, FakeCommandObject("", command="top"), session=session, group=group)
        assert any("ЭТА ГРУППА" in t or "ГРУППЕ" in t.upper() or "ТОП" in t for t in msg.answers)

    async def test_top_private_is_global(self, services, session):
        player = await make_user(session, "P")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/top")
        await rt.cmd_top(msg, FakeCommandObject("", command="top"), session=session, group=None)
        assert any("ГЛОБАЛЬН" in t.upper() for t in msg.answers)

    async def test_group_and_global_stats(self, services, session):
        player = await make_user(session, "P")
        group = await services.groups.get_or_create(-602500, "A")
        msg = FakeMessage(FakeTgUser(player.telegram_id), "/group_stats",
                          chat=FakeChat(group.telegram_chat_id))
        await rt.cmd_group_stats(msg, session=session, group=group)
        msg2 = FakeMessage(FakeTgUser(player.telegram_id), "/global_stats")
        await rt.cmd_global_stats(msg2, session=session)
        assert msg.answers and msg2.answers
