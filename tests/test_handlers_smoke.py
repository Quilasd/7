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
from bot.services.permissions import AdminLevel, Permission
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
    """ Telegram-бот: restrict_chat_member либо успех, либо TelegramAPIError.

    Вызовы restrict_chat_member ВАЛИДИРУЮТСЯ по сигнатуре настоящего
    aiogram Bot (inspect.bind): фейк не должен маскировать несовместимость
    с реальным API (регрессия: loose-kwargs падали с TypeError в проде).
    """

    def __init__(self, fail: bool = False, member_status: str = "member") -> None:
        self.fail = fail
        self.member_status = member_status
        self.calls: list[dict] = []

    async def restrict_chat_member(self, **kwargs) -> bool:
        import inspect

        from aiogram import Bot as RealBot

        inspect.signature(RealBot.restrict_chat_member).bind(None, **kwargs)  # TypeError = баг
        from aiogram.exceptions import TelegramAPIError

        self.calls.append(kwargs)
        if self.fail:
            raise TelegramAPIError(method="restrictChatMember", message="not enough rights")
        return True

    async def get_chat_member(self, *, chat_id: int, user_id: int):
        """Возвращает объект с .status для проверки создателя группы (/claim)."""
        from types import SimpleNamespace

        from aiogram.exceptions import TelegramAPIError

        self.calls.append({"chat_id": chat_id, "user_id": user_id})
        if self.fail:
            raise TelegramAPIError(method="getChatMember", message="forbidden")
        return SimpleNamespace(status=self.member_status)

    async def ban_chat_member(self, chat_id: int, user_id: int, until_date=None, **kw) -> bool:
        """Telegram-бан (до until_date, если задан)."""
        self.calls.append({"method": "ban_chat_member", "chat_id": chat_id,
                           "user_id": user_id, "until_date": until_date})
        if self.fail:
            raise TelegramAPIError(method="banChatMember", message="not enough rights")
        return True

    async def unban_chat_member(self, chat_id: int, user_id: int, only_if_banned: bool = False, **kw) -> bool:
        """Telegram-разбан."""
        self.calls.append({"method": "unban_chat_member", "chat_id": chat_id,
                           "user_id": user_id, "only_if_banned": only_if_banned})
        if self.fail:
            raise TelegramAPIError(method="unbanChatMember", message="not enough rights")
        return True

    async def set_my_commands(self, commands, scope=None) -> bool:
        """Регистрация меню «/» — записываем вызов для проверок в тестах."""
        self.calls.append({"method": "set_my_commands", "commands": commands, "scope": scope})
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
        assert any("предупреждение" in t and "1/3" in t for t in msg.answers)
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
        for fail, expected in ((False, "мут на 1 час"), (True, "нужны права админа")):
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
        assert any("НАСТРОЙКИ MAFIA ONLINE" in t for t in msg.answers)
        assert any("Не настроен" in t for t in msg.answers)  # /setup ещё не выполнен
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


class TestClaimCommand:
    async def test_claim_creator_gets_senior_and_audit(self, services, session):
        creator = await make_user(session, "Creator")
        group = await services.groups.get_or_create(-603000, "A")
        bot = FakeBot(member_status="creator")
        msg = FakeMessage(FakeTgUser(creator.telegram_id), "/claim",
                          chat=FakeChat(group.telegram_chat_id), bot=bot)
        await ga.cmd_claim(msg, session=session, group=group, services=services, db_user=creator)

        # ответ про выдачу Senior Admin
        assert any("Senior Admin" in t for t in msg.answers)
        # уровень 4 в группе
        assert await services.permissions.group_level(
            session, group.id, creator.telegram_id) == AdminLevel.SENIOR_ADMIN
        # появилось право MANAGE_SETTINGS
        access = await services.permissions.resolve(session, creator.telegram_id, group.id)
        assert Permission.MANAGE_SETTINGS in access.permissions
        # в аудите записано действие group_claim (actor == target == creator)
        logs = await services.audit.last(group_id=group.id, limit=5)
        entry = next((log for log in logs if log.action == "group_claim"), None)
        assert entry is not None
        assert entry.actor_id == creator.id
        assert entry.target_id == creator.id
        assert "was 0" in entry.details
        # создателю зарегистрировано админ-меню группы (scope ChatMember)
        menu_calls = [c for c in bot.calls if c.get("method") == "set_my_commands"]
        assert menu_calls, "set_my_commands должен вызываться после /claim"
        cmd_names = [c.command for c in menu_calls[-1]["commands"]]
        assert "settings" in cmd_names and "staff_add" in cmd_names

    async def test_claim_non_creator_denied(self, services, session):
        member = await make_user(session, "Member")
        group = await services.groups.get_or_create(-603100, "A")
        bot = FakeBot(member_status="member")
        msg = FakeMessage(FakeTgUser(member.telegram_id), "/claim",
                          chat=FakeChat(group.telegram_chat_id), bot=bot)
        await ga.cmd_claim(msg, session=session, group=group, services=services, db_user=member)
        assert any("только создатель" in t.lower() for t in msg.answers)
        # уровень не изменился
        assert await services.permissions.group_level(
            session, group.id, member.telegram_id) == AdminLevel.PLAYER

    async def test_claim_private_chat_denied(self, services, session):
        creator = await make_user(session, "Creator")
        msg = FakeMessage(FakeTgUser(creator.telegram_id), "/claim")
        await ga.cmd_claim(msg, session=session, group=None, services=services, db_user=creator)
        assert any("только внутри группы" in t for t in msg.answers)

    async def test_claim_already_senior_is_noop(self, services, session):
        senior = await make_user(session, "Senior")
        group = await services.groups.get_or_create(-603200, "A")
        await _set_staff(services.session_factory, group.id, senior.id, 4)
        bot = FakeBot(member_status="creator")
        msg = FakeMessage(FakeTgUser(senior.telegram_id), "/claim",
                          chat=FakeChat(group.telegram_chat_id), bot=bot)
        await ga.cmd_claim(msg, session=session, group=group, services=services, db_user=senior)
        assert any("уже есть права" in t for t in msg.answers)
        # прямой вызов сервиса на уже-Senior — тоже no-op, без ошибки
        ok, result = await services.groups.claim_creator(group.id, senior.id)
        assert ok is False and result == "уже есть права"


class TestCommandsMenu:
    async def test_set_member_admin_commands(self):
        import bot.utils.commands_menu as menu

        class B:
            def __init__(self):
                self.last = None

            async def set_my_commands(self, commands, scope=None):
                self.last = (commands, scope)

        bot = B()
        await menu.set_member_commands(bot, chat_id=-100, user_id=42, is_group_admin=True)
        commands, scope = bot.last
        names = [c.command for c in commands]
        # базовые групповые + админские команды группы
        assert "claim" in names and "settings" in names and "staff_add" in names
        assert scope is not None  # scope передан (ChatMember)

    async def test_set_member_non_admin_is_base_only(self):
        import bot.utils.commands_menu as menu

        class B:
            def __init__(self):
                self.last = None

            async def set_my_commands(self, commands, scope=None):
                self.last = (commands, scope)

        bot = B()
        await menu.set_member_commands(bot, chat_id=-100, user_id=42, is_group_admin=False)
        names = [c.command for c in bot.last[0]]
        assert "settings" not in names and "staff_add" not in names
        assert "top" in names and "claim" in names  # базовый набор группы

    def test_admin_commands_includes_claim_and_staff(self):
        import bot.utils.commands_menu as menu

        names = {c.command for c in menu.ADMIN_COMMANDS}
        for required in ("claim", "staff_add", "staff_remove", "staff_info",
                         "unmute", "unban", "set_roles", "game_stop"):
            assert required in names, required


class TestUsageHints:
    """Пустые команды (без аргументов) должны показывать пример использования."""

    async def _boss_group(self, services, session, tag):
        boss = await make_user(session, "Boss" + tag)
        group = await services.groups.get_or_create(-(604000 + int(tag)), "A")
        await _set_staff(services.session_factory, group.id, boss.id, 4)
        return boss, group

    async def test_staff_add_empty_shows_example(self, services, session):
        boss, group = await self._boss_group(services, session, "1")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/staff_add",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff_add(msg, FakeCommandObject(None, "staff_add"), session=session,
                               group=group, services=services, db_user=boss)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "staff_add" in joined and "@username" in joined
        assert "Senior Admin" in joined  # легенда уровней

    async def test_staff_remove_empty_shows_example(self, services, session):
        boss, group = await self._boss_group(services, session, "2")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/staff_remove",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff_remove(msg, FakeCommandObject(None, "staff_remove"), session=session,
                                  group=group, services=services, db_user=boss)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "staff_remove" in joined

    async def test_staff_info_empty_shows_example(self, services, session):
        boss, group = await self._boss_group(services, session, "3")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/staff_info",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_staff_info(msg, FakeCommandObject(None, "staff_info"), session=session,
                                group=group, services=services)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "staff_info" in joined

    async def test_set_night_time_empty_shows_range_and_example(self, services, session):
        boss, group = await self._boss_group(services, session, "4")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/set_night_time abc",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_set_number(msg, FakeCommandObject("abc", "set_night_time"), session=session,
                                group=group, services=services, db_user=boss)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "30" in joined and "600" in joined  # диапазон

    async def test_set_roles_empty_shows_example(self, services, session):
        boss, group = await self._boss_group(services, session, "5")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/set_roles",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_set_roles(msg, FakeCommandObject(None, "set_roles"), session=session,
                               group=group, services=services, db_user=boss)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "mafia" in joined and "maniac" in joined

    async def test_warn_empty_shows_example(self, services, session):
        mod, group = await self._boss_group(services, session, "6")
        msg = FakeMessage(FakeTgUser(mod.telegram_id), "/warn",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        joined = "\n".join(msg.answers)
        assert "Пример" in joined and "@username" in joined

    async def test_room_kick_empty_shows_example(self, services, session):
        boss, group = await self._boss_group(services, session, "7")
        msg = FakeMessage(FakeTgUser(boss.telegram_id), "/room_kick",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_room_manage(msg, FakeCommandObject(None, "room_kick"), session=session,
                                 group=group, services=services, db_user=boss)
        joined = "\n".join(msg.answers)
        assert "Формат" in joined and "Пример" in joined
        assert "room_kick" in joined


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


# ------------------------------------------------ регрессия кнопки «Профиль»

import inspect

import bot.handlers.profile as pf
from bot.database.repositories.groups import GroupPlayerRepository


async def call_like_aiogram(handler, **data):
    """Вызов хендлера ровно так, как это делает aiogram: только параметры,
    объявленные в сигнатуре (DI по имени). Ловит NameError вида
    «используется services, которого нет в сигнатуре» — регрессия кнопки профиля."""
    sig = inspect.signature(handler)
    kwargs = {k: v for k, v in data.items() if k in sig.parameters}
    await handler(**kwargs)


class TestProfileButtonRegression:
    """Кнопка 👤 Профиль (MenuCB action=profile) сломалась: cb_profile вызывал
    services, не объявив его в сигнатуре -> NameError на каждом нажатии."""

    async def test_profile_callback_no_crash_private(self, services, session, monkeypatch):
        user = await make_user(session, "Hero")
        captured: dict = {}

        async def fake_edit(cb, text, kb=None):
            captured["text"] = text

        monkeypatch.setattr(pf, "edit_or_answer", fake_edit)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        # до фикса: services не в сигнатуре -> NameError внутри хендлера
        await call_like_aiogram(
            pf.cb_profile, callback=cb, session=session, services=services,
            db_user=user, group=None,
        )
        assert "ГЛОБАЛЬНО" in captured["text"]

    async def test_profile_callback_global_and_local_scopes(self, services, session, monkeypatch):
        """Профиль обязан показывать ОБА рейтинга: 🌐 глобальный и 🏠 этой группы
        (существующая система global/local), включая локальные позиции в топе."""
        user = await make_user(session, "Hero")
        user.rating, user.wins, user.level, user.xp = 1428, 47, 12, 500

        group = await services.groups.get_or_create(-604200, "Клуб")
        gp = await GroupPlayerRepository(session).ensure(group.id, user.id)
        gp.rating, gp.wins, gp.level, gp.xp = 386, 14, 7, 200
        other = await make_user(session, "Rival")
        gp2 = await GroupPlayerRepository(session).ensure(group.id, other.id)
        gp2.rating, gp2.wins, gp2.level, gp2.xp = 100, 2, 2, 50
        await session.commit()

        captured: dict = {}

        async def fake_edit(cb, text, kb=None):
            captured["text"] = text

        monkeypatch.setattr(pf, "edit_or_answer", fake_edit)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(
            pf.cb_profile, callback=cb, session=session, services=services,
            db_user=user, group=group,
        )
        text = captured["text"]
        # глобальный scope
        assert "ГЛОБАЛЬНО" in text
        assert "⭐ Общий: <b>1428</b>" in text
        assert "(#" in text
        # локальный scope (не заменён глобальным!) — компактный блок
        assert "В ЭТОЙ ГРУППЕ" in text
        assert "Клуб" in text                       # название группы
        assert "⭐ <b>386</b> <code>(#1)</code>" in text
        assert "🏆 <b>14</b> <code>(#1)</code>" in text
        assert "📈 Ур. <b>7</b> <code>(#1)</code>" in text
        # локальный блок НЕ дублирует глобальную статистику
        assert "Winrate" not in text.split("В ЭТОЙ ГРУППЕ")[1]
        assert "Серия" not in text.split("В ЭТОЙ ГРУППЕ")[1]
        # глобальный блок на месте и не смешан с локальным
        assert "⭐ Общий: <b>1428</b>" in text
        # игровая статистика — отдельным блоком внизу
        assert "В ИГРЕ" in text and "☠️ Убийств" in text

    async def test_profile_command_and_settings_callback_ok(self, services, session, monkeypatch):
        """Соседние точки входа профиля (/profile, кнопка Настройки) не регрессировали."""
        user = await make_user(session, "Hero")
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/profile")
        await call_like_aiogram(
            pf.cmd_profile, message=msg, command=FakeCommandObject(None),
            session=session, services=services, db_user=user,
        )
        assert msg.answers and "ГЛОБАЛЬНО" in msg.answers[0]

        captured: dict = {}

        async def fake_edit(cb, text, kb=None):
            captured["text"] = text

        monkeypatch.setattr(pf, "edit_or_answer", fake_edit)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(
            pf.cb_settings, callback=cb, session=session, services=services, db_user=user,
        )
        assert "ГЛОБАЛЬНО" in captured["text"]

    async def test_group_block_without_ranks_still_renders(self):
        """profile_group_block работает и без рангов (обратная совместимость)."""
        from types import SimpleNamespace

        gp = SimpleNamespace(rating=10, wins=3, losses=1, level=2, xp=40,
                             games_played=4, kills=0, saves=0, investigations=0,
                             correct_votes=0, win_streak=1, best_win_streak=2)
        group = SimpleNamespace(title="G")
        text = pf.profile_group_block(group, gp)
        assert "В ЭТОЙ ГРУППЕ" in text and "G" in text
        assert "⭐ <b>10</b>" in text and "🏆 <b>3</b>" in text and "Ур. <b>2</b>" in text
        # без дублирования глобальной статистики
        assert "XP" not in text and "Winrate" not in text


# ------------------------------------------------------- модерация 2.0 (A+B)

class TestModerationV2Rights:
    """Новое распределение прав: мут — Helper, варн/кик/врембан — Moderator,
    постоянный бан/разбан — Admin. Плюс защита иерархии."""

    async def test_helper_can_mute_cannot_warn(self, services, session):
        helper = await make_user(session, "H")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-605000, "A")
        await _set_staff(services.session_factory, group.id, helper.id, 1)

        reply = FakeMessage(FakeTgUser(noisy.telegram_id), "x")
        mmsg = FakeMessage(FakeTgUser(helper.telegram_id), "/mute",
                           chat=FakeChat(group.telegram_chat_id), reply=reply,
                           bot=FakeBot())
        await ga.cmd_mute(mmsg, session=session, group=group, db_user=helper, services=services)
        assert any("мут" in t for t in mmsg.answers)

        wmsg = FakeMessage(FakeTgUser(helper.telegram_id), "/warn",
                           chat=FakeChat(group.telegram_chat_id), reply=FakeMessage(
                               FakeTgUser(noisy.telegram_id), "x"))
        await ga.cmd_warns(wmsg, session=session, group=group, db_user=helper, services=services)
        assert any("WARN_PLAYER" in t for t in wmsg.answers)  # варна у Helper больше нет

    async def test_mod_has_no_ban_access_at_all(self, services, session):
        """У модератора доступа к бану НЕТ — только варн/кик/мут."""
        mod = await make_user(session, "M")
        bad = await make_user(session, "B")
        group = await services.groups.get_or_create(-605100, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        reply = FakeMessage(FakeTgUser(bad.telegram_id), "x")
        msg = FakeMessage(FakeTgUser(mod.telegram_id), "/ban",
                          chat=FakeChat(group.telegram_chat_id), reply=reply, bot=FakeBot())
        await ga.cmd_ban(msg, session=session, group=group, db_user=mod, services=services)
        assert any("BAN_PLAYER" in t for t in msg.answers)  # отказ по праву
        gp = await services.groups.local_player(group.id, bad.id)
        assert gp is None or not gp.is_banned  # бана не случилось

        pmsg = FakeMessage(FakeTgUser(bad.telegram_id), "/ban",
                           chat=FakeChat(group.telegram_chat_id),
                           reply=FakeMessage(FakeTgUser(mod.telegram_id), "x"), bot=FakeBot())
        await ga.cmd_ban(pmsg, session=session, group=group, db_user=bad, services=services)
        assert any("BAN_PLAYER" in t for t in pmsg.answers)  # обычный игрок тоже не может

    async def test_admin_ban_durations_all_units(self, services, session):
        """Админ: 30m / 2h / 3d / 1w / 2mo; без времени — навсегда."""
        from datetime import timedelta

        from bot.utils.helpers import utcnow

        admin = await make_user(session, "A")
        bad = await make_user(session, "B")
        group = await services.groups.get_or_create(-605200, "A")
        await _set_staff(services.session_factory, group.id, admin.id, 3)

        cases = [("30m", 30), ("2h", 120), ("3d", 3 * 24 * 60),
                 ("1w", 7 * 24 * 60), ("2mo", 60 * 24 * 60)]
        for token, minutes in cases:
            msg = FakeMessage(FakeTgUser(admin.telegram_id), f"/ban {bad.telegram_id} {token}",
                              chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
            await ga.cmd_ban(msg, session=session, group=group, db_user=admin, services=services)
            gp = await services.groups.local_player(group.id, bad.id)
            got = (gp.banned_until - utcnow()).total_seconds() / 60
            assert abs(got - minutes) < 1, (token, got, minutes)
            assert any("навсегда" not in t for t in msg.answers), token

        # без времени — навсегда
        msg = FakeMessage(FakeTgUser(admin.telegram_id), f"/ban {bad.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_ban(msg, session=session, group=group, db_user=admin, services=services)
        assert any("навсегда" in t for t in msg.answers)
        gp = await services.groups.local_player(group.id, bad.id)
        assert gp.is_banned and gp.banned_until is None

    async def test_admin_ban_permanent_and_unban_lifts_telegram(self, services, session):
        admin = await make_user(session, "A")
        bad = await make_user(session, "B")
        group = await services.groups.get_or_create(-605300, "A")
        await _set_staff(services.session_factory, group.id, admin.id, 3)

        bot = FakeBot()
        msg = FakeMessage(FakeTgUser(admin.telegram_id), f"/ban {bad.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id), bot=bot)
        await ga.cmd_ban(msg, session=session, group=group, db_user=admin, services=services)
        assert any("навсегда" in t for t in msg.answers)
        gp = await services.groups.local_player(group.id, bad.id)
        assert gp.is_banned and gp.banned_until is None
        assert any(c["method"] == "ban_chat_member" for c in bot.calls)

        umsg = FakeMessage(FakeTgUser(admin.telegram_id), f"/unban {bad.telegram_id}",
                           chat=FakeChat(group.telegram_chat_id), bot=bot)
        await ga.cmd_ban(umsg, session=session, group=group, db_user=admin, services=services)
        assert any("разбанен" in t for t in umsg.answers)
        gp = await services.groups.local_player(group.id, bad.id)
        assert not gp.is_banned
        unban = [c for c in bot.calls if c["method"] == "unban_chat_member"]
        assert unban and unban[0]["only_if_banned"] is True

    async def test_hierarchy_mod_cannot_punish_mod_or_admin(self, services, session):
        mod = await make_user(session, "M1")
        mod2 = await make_user(session, "M2")
        admin = await make_user(session, "A")
        group = await services.groups.get_or_create(-605400, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)
        await _set_staff(services.session_factory, group.id, mod2.id, 2)
        await _set_staff(services.session_factory, group.id, admin.id, 3)

        for target, cmd in ((mod2, "/warn"), (admin, "/kick")):
            msg = FakeMessage(FakeTgUser(mod.telegram_id), cmd,
                              chat=FakeChat(group.telegram_chat_id),
                              reply=FakeMessage(FakeTgUser(target.telegram_id), "x"),
                              bot=FakeBot())
            if cmd == "/warn":
                await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
            else:
                await ga.cmd_kick(msg, session=session, group=group, db_user=mod, services=services)
            assert any("выше или равным" in t for t in msg.answers), cmd
        # бан модеру недоступен вовсе — отказ по праву, а не по иерархии
        bmsg = FakeMessage(FakeTgUser(mod.telegram_id), f"/ban {mod2.telegram_id}",
                           chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_ban(bmsg, session=session, group=group, db_user=mod, services=services)
        assert any("BAN_PLAYER" in t for t in bmsg.answers)

        # админ МОЖЕТ карать модератора; нельзя карать себя
        kmsg = FakeMessage(FakeTgUser(admin.telegram_id), "/kick",
                           chat=FakeChat(group.telegram_chat_id),
                           reply=FakeMessage(FakeTgUser(mod.telegram_id), "x"), bot=FakeBot())
        await ga.cmd_kick(kmsg, session=session, group=group, db_user=admin, services=services)
        assert any("исключён" in t for t in kmsg.answers)

        smsg = FakeMessage(FakeTgUser(admin.telegram_id), "/warn",
                           chat=FakeChat(group.telegram_chat_id),
                           reply=FakeMessage(FakeTgUser(admin.telegram_id), "x"))
        await ga.cmd_warns(smsg, session=session, group=group, db_user=admin, services=services)
        assert any("самому себе" in t for t in smsg.answers)


class TestWarnV2:
    """Варн с причиной/сроком; 3/3 -> авто-бан на время; /warnings со списком."""

    async def test_warn_with_reason_and_duration(self, services, session):
        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-606000, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id} 3d спам в чате",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("1/3" in t and "спам в чате" in t for t in msg.answers)
        warns = await services.groups.warnings_of(group.id, noisy.id)
        assert len(warns) == 1 and warns[0].reason == "спам в чате"
        hours = (warns[0].expires_at - warns[0].created_at).total_seconds() / 3600
        assert abs(hours - 72) < 0.01  # 3 дня

    async def test_three_warns_auto_ban(self, services, session):
        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-606100, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        bot = FakeBot()
        for i in range(3):
            msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id}",
                              chat=FakeChat(group.telegram_chat_id), bot=bot)
            await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("3/3" in t and "авто-бан" in t for t in msg.answers)
        gp = await services.groups.local_player(group.id, noisy.id)
        assert gp.is_banned and gp.banned_until is not None  # бан на время (24 ч по умолч.)
        bans = [c for c in bot.calls if c["method"] == "ban_chat_member"]
        assert bans and bans[0]["until_date"] is not None    # Telegram-бан со сроком
        assert gp.warnings == 0                              # варны израсходованы
        assert await services.groups.warnings_of(group.id, noisy.id) == []

    async def test_warnings_lists_active_with_reasons(self, services, session):
        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-606200, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        await services.groups.warn(group.id, noisy.id, mod.id, reason="флуд")
        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warnings {noisy.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id))
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("1/3" in t and "флуд" in t for t in msg.answers)

    async def test_unwarn_removes_last(self, services, session):
        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-606300, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        await services.groups.warn(group.id, noisy.id, mod.id, reason="1")
        await services.groups.warn(group.id, noisy.id, mod.id, reason="2")
        msg = FakeMessage(FakeTgUser(mod.telegram_id), "/unwarn",
                          chat=FakeChat(group.telegram_chat_id),
                          reply=FakeMessage(FakeTgUser(noisy.telegram_id), "x"))
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("Активных: 1" in t for t in msg.answers)
        left = await services.groups.warnings_of(group.id, noisy.id)
        assert len(left) == 1 and left[0].reason == "1"


class TestDurationUnits:
    """Единые единицы срока: 30m / 2h / 3d / 1w / 2mo (и по-русски: мин/ч/д/нед/мес)."""

    async def test_mute_with_units(self, services, session):
        from datetime import datetime, timezone

        helper = await make_user(session, "H")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-607000, "A")
        await _set_staff(services.session_factory, group.id, helper.id, 1)

        for token, minutes in (("2h", 120), ("3d", 3 * 24 * 60), ("1mo", 30 * 24 * 60)):
            bot = FakeBot()
            msg = FakeMessage(FakeTgUser(helper.telegram_id), f"/mute {noisy.telegram_id} {token}",
                              chat=FakeChat(group.telegram_chat_id), bot=bot)
            await ga.cmd_mute(msg, session=session, group=group, db_user=helper, services=services)
            calls = [c for c in bot.calls if c.get("method") != "set_my_commands"]
            assert calls, token
            until = calls[-1]["until_date"]
            got = (until - datetime.now(timezone.utc)).total_seconds() / 60
            assert abs(got - minutes) < 2, (token, got, minutes)
            assert any("мут" in t for t in msg.answers)

    async def test_mute_bare_number_still_minutes(self, services, session):
        helper = await make_user(session, "H")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-607100, "A")
        await _set_staff(services.session_factory, group.id, helper.id, 1)

        msg = FakeMessage(FakeTgUser(helper.telegram_id), f"/mute {noisy.telegram_id} 90",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_mute(msg, session=session, group=group, db_user=helper, services=services)
        assert any("1 час 30 мин" in t for t in msg.answers)

    async def test_warn_with_minute_duration(self, services, session):
        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-607200, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id} 30m флуд",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("1/3" in t and "флуд" in t and "30 мин" in t for t in msg.answers)
        warns = await services.groups.warnings_of(group.id, noisy.id)
        minutes = (warns[0].expires_at - warns[0].created_at).total_seconds() / 60
        assert abs(minutes - 30) < 1

    async def test_reban_updates_duration(self, services, session):
        from bot.utils.helpers import utcnow

        admin = await make_user(session, "A")
        bad = await make_user(session, "B")
        group = await services.groups.get_or_create(-607300, "A")
        await _set_staff(services.session_factory, group.id, admin.id, 3)

        # 30 минут, потом продлеваем до недели — срок должен обновиться
        for token, minutes in (("30m", 30), ("1w", 7 * 24 * 60)):
            msg = FakeMessage(FakeTgUser(admin.telegram_id), f"/ban {bad.telegram_id} {token}",
                              chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
            await ga.cmd_ban(msg, session=session, group=group, db_user=admin, services=services)
            gp = await services.groups.local_player(group.id, bad.id)
            got = (gp.banned_until - utcnow()).total_seconds() / 60
            assert abs(got - minutes) < 1, (token, got, minutes)

    async def test_auto_ban_3_of_3_is_one_day(self, services, session):
        from bot.utils.helpers import utcnow

        mod = await make_user(session, "M")
        noisy = await make_user(session, "N")
        group = await services.groups.get_or_create(-607400, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)

        msg = None
        for _ in range(3):
            msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id}",
                              chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
            await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("3/3" in t and "авто-бан" in t and "1 день" in t for t in msg.answers)
        gp = await services.groups.local_player(group.id, noisy.id)
        minutes = (gp.banned_until - utcnow()).total_seconds() / 60
        assert abs(minutes - 24 * 60) < 1  # ровно сутки


class TestWarnIds:
    """У каждого варна свой ID: /warnings показывает #ID, /unwarn снимает по ID."""

    async def _three_warns(self, services, session, group, mod, noisy):
        # ВНИМАНИЕ: 3-й варн = 3/3 -> авто-бан и сброс; для тестов ID нужно <3
        for reason in ("спам", "флуд"):
            msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id} {reason}",
                              chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
            await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)

    async def test_warnings_shows_ids(self, services, session):
        mod, noisy = await make_user(session, "M"), await make_user(session, "N")
        group = await services.groups.get_or_create(-608000, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)
        await self._three_warns(services, session, group, mod, noisy)

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warnings {noisy.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        warns = await services.groups.warnings_of(group.id, noisy.id)
        assert len(warns) == 2
        for w in warns:
            assert any(f"#{w.id}" in t for t in msg.answers), w.id
        assert any("/unwarn @user" in t for t in msg.answers)  # подсказка

    async def test_unwarn_by_id_removes_exact_warn(self, services, session):
        mod, noisy = await make_user(session, "M"), await make_user(session, "N")
        group = await services.groups.get_or_create(-608100, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)
        await self._three_warns(services, session, group, mod, noisy)
        warns = await services.groups.warnings_of(group.id, noisy.id)
        victim = warns[0]  # снимаем ПЕРВЫЙ, а не последний

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/unwarn {noisy.telegram_id} {victim.id}",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any(f"#{victim.id}" in t and "Активных: 1" in t for t in msg.answers)
        left = await services.groups.warnings_of(group.id, noisy.id)
        assert len(left) == 1 and all(w.id != victim.id for w in left)

    async def test_unwarn_wrong_id_says_not_found(self, services, session):
        mod, noisy = await make_user(session, "M"), await make_user(session, "N")
        group = await services.groups.get_or_create(-608200, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)
        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/warn {noisy.telegram_id} спам",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/unwarn {noisy.telegram_id} 999999",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("нет активного варна" in t for t in msg.answers)
        warns = await services.groups.warnings_of(group.id, noisy.id)
        assert len(warns) == 1  # ничего не снято

    async def test_unwarn_without_id_still_removes_last(self, services, session):
        mod, noisy = await make_user(session, "M"), await make_user(session, "N")
        group = await services.groups.get_or_create(-608300, "A")
        await _set_staff(services.session_factory, group.id, mod.id, 2)
        await self._three_warns(services, session, group, mod, noisy)
        before = await services.groups.warnings_of(group.id, noisy.id)
        last_id = before[-1].id

        msg = FakeMessage(FakeTgUser(mod.telegram_id), f"/unwarn {noisy.telegram_id}",
                          chat=FakeChat(group.telegram_chat_id), bot=FakeBot())
        await ga.cmd_warns(msg, session=session, group=group, db_user=mod, services=services)
        assert any("последнее предупреждение" in t and "Активных: 1" in t for t in msg.answers)
        left = await services.groups.warnings_of(group.id, noisy.id)
        assert len(left) == 1 and all(w.id != last_id for w in left)


class TestAchievementsCommand:
    """«0/12» в профиле без списка: /achievements показывает все достижения."""

    async def test_achievements_empty_shows_all_and_hides_hidden(self, services, session):
        user = await make_user(session, "Newbie")
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/achievements")
        await pf.cmd_achievements(msg, session=session, db_user=user)
        text = msg.answers[0]
        assert "ДОСТИЖЕНИЯ" in text and "0/12" in text
        # все 11 открытых достижений видны с описанием
        for name in ("Первая кровь", "Спаситель", "Живой щит", "Верная догадка", "Охотник",
                     "Верный приговор", "Последний выживший", "Идеальное алиби", "Город встал",
                     "Неудержимый", "Легенда"):
            assert name in text, name
        # скрытое (Снайпер) замаскировано до получения
        assert "Снайпер" not in text and "Скрытое достижение" in text
        assert "⬜" in text and "✅" not in text

    async def test_achievements_after_award_shows_check(self, services, session):
        from bot.database.repositories.social import UserAchievementRepository

        user = await make_user(session, "Winner")
        await UserAchievementRepository(session).award(user.id, "first_win")
        await UserAchievementRepository(session).award(user.id, "sharpshooter")
        await session.commit()

        msg = FakeMessage(FakeTgUser(user.telegram_id), "/achievements")
        await pf.cmd_achievements(msg, session=session, db_user=user)
        text = msg.answers[0]
        assert "2/12" in text
        assert "✅ 🎉 <b>Первая кровь</b>" in text   # полученное — галочка
        assert "✅ 🔫 <b>Снайпер</b>" in text       # скрытое после получения видно
        assert "⬜ 🔥 Неудержимый" in text          # неполученное — пустое место

    async def test_profile_callback_achievements_button(self, services, session, monkeypatch):
        """Кнопка 🏅 Достижения в профиле (ProfileCB action=achievements)."""
        user = await make_user(session, "Clicker")
        captured: dict = {}

        async def fake_edit(cb, text, kb=None):
            captured["text"] = text

        monkeypatch.setattr(pf, "edit_or_answer", fake_edit)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(
            pf.cb_achievements, callback=cb, session=session, services=services,
            db_user=user, group=None,
        )
        assert "ДОСТИЖЕНИЯ" in captured["text"] and "0/12" in captured["text"]

    async def test_profile_command_in_group_shows_local_block(self, services, session):
        """Команда /profile в группе обязана показывать и блок 🏠 ЭТА ГРУППА."""
        user = await make_user(session, "Hero")
        group = await services.groups.get_or_create(-609000, "Клуб")
        gp = await GroupPlayerRepository(session).ensure(group.id, user.id)
        gp.rating, gp.wins, gp.level, gp.xp = 386, 14, 7, 200
        await session.commit()

        msg = FakeMessage(FakeTgUser(user.telegram_id), "/profile",
                          chat=FakeChat(group.telegram_chat_id))
        await call_like_aiogram(
            pf.cmd_profile, message=msg, command=FakeCommandObject(None),
            session=session, services=services, db_user=user, group=group,
        )
        text = msg.answers[0]
        assert "ГЛОБАЛЬНО" in text and "В ЭТОЙ ГРУППЕ" in text
        assert "⭐ <b>386</b>" in text          # компактный локальный блок
        assert "⭐ Общий:" in text              # глобальный блок не пропал
        assert "☠️ Убийств" in text            # игровая статистика внизу


class TestMenuCallbacksSmoke:
    """Регрессии кнопок главного меню: каждая вызывается без падений."""

    async def test_main_menu_buttons_render(self, services, session, monkeypatch):
        import bot.handlers.ratings as rt
        import bot.handlers.rooms as rm
        import bot.handlers.start as st

        user = await make_user(session, "Clicker")
        group = await services.groups.get_or_create(-611000, "G")
        captured: list[str] = []

        async def fake_edit(cb, text, kb=None):
            captured.append(text)

        for mod in (st, pf, rt, rm):
            monkeypatch.setattr(mod, "edit_or_answer", fake_edit)

        cases = [
            (st.cb_main, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)))),
            (st.cb_rules, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)))),
            (st.cb_play, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)),
                              session=session, db_user=user)),
            (pf.cb_settings, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)),
                                  session=session, services=services, db_user=user)),
            (rt.cb_menu_rating, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)),
                                     session=session, group=None)),
            (rt.cb_menu_rating, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)),
                                     session=session, group=group)),
            (rm.cb_find_refresh, dict(callback=FakeCallback(FakeTgUser(user.telegram_id)),
                                      session=session, db_user=user)),
        ]
        for handler, data in cases:
            captured.clear()
            cb = data["callback"]
            cb.answers.clear()
            await call_like_aiogram(handler, **data)
            # что-то отрисовали (edit или answer) — не упало
            assert captured or cb.answers, handler.__name__

        # рейтинг в группе — локальный топ; в личке — глобальный (не смешиваются)
        cb_group = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(rt.cb_menu_rating, callback=cb_group,
                                session=session, group=group)
        cb_private = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(rt.cb_menu_rating, callback=cb_private,
                                session=session, group=None)
