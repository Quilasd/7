"""Справка /cmdhelp и /acmdhelp: реестр команд, уровни, изоляция chat_id.

TEST 1–20 из ТЗ:
1. обычный игрок → /cmdhelp: игровые есть, админских нет;
2-6. Lv.1..Lv.4 и глобальный Owner → /acmdhelp по нарастающей;
7-8. права группы A не действуют в группе B;
9. обычный игрок → /acmdhelp: отказ без раскрытия списка;
10-12. ЛС: /cmdhelp работает, /acmdhelp отказ/по глобальному уровню;
13-15. /start, /setup, /settings не изменены;
16. callback'и без конфликтов;
17-18. вызовы не создают записей в БД;
19. все команды реестра реально существуют в handlers;
20. пороги уровней реестра совпадают с LEVEL_PERMISSIONS.
"""

from __future__ import annotations

import pytest

from bot.handlers import cmdhelp as ch
from bot.services.permissions import AdminLevel, LEVEL_PERMISSIONS, Permission
from bot.utils.callbacks import SetupCB
from bot.utils.command_registry import (
    ADMIN_COMMANDS,
    PLAYER_COMMANDS,
    admin_help_text,
    min_level,
    player_help_text,
)
from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser
from tests.test_handlers_smoke import call_like_aiogram
from tests.conftest import make_user


async def _msg(user, text, chat_id=None, chat_type="private"):
    chat_id = chat_id if chat_id is not None else user.telegram_id
    return FakeMessage(FakeTgUser(user.telegram_id), text,
                       chat=FakeChat(chat_id, chat_type))


async def _db_rows(session) -> int:
    """Суммарное число строк в ключевых таблицах (детектор лишних записей)."""
    from sqlalchemy import func, select

    from bot.database.models import Group, GroupPlayer, GroupSettingsModel

    total = 0
    for model in (Group, GroupPlayer, GroupSettingsModel):
        total += int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )
    await session.commit()
    return total


def _names(text: str) -> set[str]:
    return set(re.findall(r"/([a-z_]+)", text))


class TestCmdhelp:
    """TEST 1, 10: /cmdhelp — только игровые команды."""

    async def test_player_help_no_admin_commands(self, services, session):
        user = await make_user(session, "Player")
        msg = await _msg(user, "/cmdhelp")
        await call_like_aiogram(ch.cmd_cmdhelp, message=msg)
        text = msg.answers[0]
        # базовые игровые команды есть
        assert "КОМАНДЫ MAFIA ONLINE" in text
        for cmd in ("start", "profile", "top", "history", "friends", "cmdhelp"):
            assert f"/{cmd}" in text, cmd
        # админские команды скрыты
        for forbidden in ("ban", "mute", "warn", "staff_add", "settings",
                          "broadcast", "maintenance", "owner", "admin",
                          "set_rating", "reward_grant"):
            assert f"/{forbidden}" not in text, forbidden

    async def test_cmdhelp_in_private_hides_group_only(self, services, session):
        """TEST 10: в ЛС групповые команды (/group_stats, /claim) скрыты."""
        user = await make_user(session, "Solo")
        msg = await _msg(user, "/cmdhelp")
        await call_like_aiogram(ch.cmd_cmdhelp, message=msg)
        assert "/group_stats" not in msg.answers[0]
        assert "/claim" not in msg.answers[0]

    async def test_cmdhelp_in_group_shows_group_commands(self, services, session):
        user = await make_user(session, "Groupie")
        group = await services.groups.get_or_create(-700001, "G")
        msg = await _msg(user, "/cmdhelp", chat_id=group.telegram_chat_id,
                         chat_type="supergroup")
        await call_like_aiogram(ch.cmd_cmdhelp, message=msg)
        assert "/group_stats" in msg.answers[0]
        assert "/claim" in msg.answers[0]

    async def test_cmdhelp_no_db_writes(self, services, session):
        """TEST 17: повторные вызовы не создают записей в БД."""
        user = await make_user(session, "Clean")
        before = await _db_rows(session)
        for _ in range(3):
            msg = await _msg(user, "/cmdhelp")
            await call_like_aiogram(ch.cmd_cmdhelp, message=msg)
        assert await _db_rows(session) == before


class TestAcmdhelpLevels:
    """TEST 2–6: нарастающий список по уровню."""

    async def _acmd(self, services, session, user, group=None, chat_id=None):
        msg = await _msg(
            user, "/acmdhelp",
            chat_id=chat_id if chat_id is not None else user.telegram_id,
            chat_type="supergroup" if group else "private",
        )
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session,
            services=services, group=group,
        )
        return msg.answers[0] if msg.answers else ""

    async def _set_level(self, services, session, user, group, level):
        await session.commit()
        await services.groups.set_staff(
            group.id, user.telegram_id, AdminLevel.OWNER, user.id, level, user.id
        )

    async def test_lv1_sees_only_lv1(self, services, session):
        """TEST 2: Lv.1 — только команды уровня 1."""
        user = await make_user(session, "Helper1")
        group = await services.groups.get_or_create(-710001, "A")
        await self._set_level(services, session, user, group, 1)
        text = await self._acmd(services, session, user, group)
        assert "АДМИНИСТРАТИВНЫЕ КОМАНДЫ" in text
        assert "Lv.1" in text  # уровень пользователя указан
        for cmd in ("players", "player", "mute", "unmute", "staff", "staff_info",
                    "game", "rooms", "botstats", "game_players", "game_phase",
                    "acmdhelp"):
            assert f"/{cmd}" in text, cmd
        for hidden in ("warn", "kick", "ban", "settings", "staff_add",
                       "game_stop", "owner", "admin", "maintenance"):
            assert f"/{hidden}" not in text, hidden

    async def test_lv2_adds_warn_kick(self, services, session):
        """TEST 3: Lv.2 = Lv.1 + варны/кик."""
        user = await make_user(session, "Mod2")
        group = await services.groups.get_or_create(-710002, "A")
        await self._set_level(services, session, user, group, 2)
        text = await self._acmd(services, session, user, group)
        for cmd in ("warn", "unwarn", "warnings", "kick", "mute", "players"):
            assert f"/{cmd}" in text, cmd
        for hidden in ("ban", "settings", "staff_add", "game_stop", "owner"):
            assert f"/{hidden}" not in text, hidden

    async def test_lv3_adds_ban_games(self, services, session):
        """TEST 4: Lv.3 = Lv.1+2 + бан/игры/комнаты/debug."""
        user = await make_user(session, "Admin3")
        group = await services.groups.get_or_create(-710003, "A")
        await self._set_level(services, session, user, group, 3)
        text = await self._acmd(services, session, user, group)
        for cmd in ("ban", "unban", "game_stop", "createroom", "room_force_start",
                    "testgame", "warn", "kick", "mute"):
            assert f"/{cmd}" in text, cmd
        for hidden in ("settings", "staff_add", "broadcast", "owner", "admin"):
            assert f"/{hidden}" not in text, hidden

    async def test_lv4_adds_settings_staff_broadcast(self, services, session):
        """TEST 5: Lv.4 = + настройки/штаб/broadcast; глобальных нет."""
        user = await make_user(session, "Senior4")
        group = await services.groups.get_or_create(-710004, "A")
        await self._set_level(services, session, user, group, 4)
        text = await self._acmd(services, session, user, group)
        for cmd in ("settings", "set_roles", "staff_add", "staff_remove",
                    "broadcast", "logs", "setup", "ban", "mute"):
            assert f"/{cmd}" in text, cmd
        # локальный Lv.4 НЕ видит глобальные панели (они только для .env-админов)
        for hidden in ("owner", "admin", "reload", "reward_grant", "maintenance"):
            assert f"/{hidden}" not in text, hidden

    async def test_global_owner_sees_everything(self, services, session, monkeypatch):
        """TEST 6: глобальный Owner Lv.5 — полный доступ (в группе, без
        локальной записи Lv.5)."""
        owner = await make_user(session, "God")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
        group = await services.groups.get_or_create(-710005, "A")
        await session.commit()
        text = await self._acmd(services, session, owner, group)
        for cmd in ("owner", "admin", "maintenance", "achievement_grant",
                    "set_rating", "debug_help", "settings", "staff_add",
                    "ban", "mute", "setup", "broadcast"):
            assert f"/{cmd}" in text, cmd
        assert "глобальный" in text
        # Lv.5-запись для Owner в группе НЕ создаётся
        from bot.database.repositories.groups import GroupAdminRepository

        assert await GroupAdminRepository(session).level_of(group.id, owner.id) == 0


class TestAcmdhelpIsolation:
    """TEST 7–8: изоляция групп по chat_id."""

    async def test_lv3_in_group_b_uses_b_rights(self, services, session):
        """TEST 7: Lv.3 в A, вызов в B → права B (обычный игрок → отказ)."""
        user = await make_user(session, "Cross")
        group_a = await services.groups.get_or_create(-720001, "A")
        group_b = await services.groups.get_or_create(-720002, "B")
        await session.commit()
        await services.groups.set_staff(
            group_a.id, user.telegram_id, AdminLevel.OWNER, user.id, 3, user.id
        )
        # вызов в группе B: прав A там нет
        msg = await _msg(user, "/acmdhelp", chat_id=group_b.telegram_chat_id,
                         chat_type="supergroup")
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session, services=services,
            group=group_b,
        )
        assert "только администраторам" in msg.answers[0]

    async def test_admin_of_a_is_player_in_b(self, services, session):
        """TEST 8: resolve уровня в группе B не видит роли группы A."""
        user = await make_user(session, "Dual")
        group_a = await services.groups.get_or_create(-720003, "A")
        group_b = await services.groups.get_or_create(-720004, "B")
        await session.commit()
        await services.groups.set_staff(
            group_a.id, user.telegram_id, AdminLevel.OWNER, user.id, 4, user.id
        )
        level_a = await services.permissions.group_level(session, group_a.id, user.telegram_id)
        level_b = await services.permissions.group_level(session, group_b.id, user.telegram_id)
        assert level_a == 4
        assert level_b == 0  # в B он обычный игрок

    async def test_admin_a_with_lower_admin_b_shows_b_level(self, services, session):
        """Lv.4 в A и Lv.1 в B → в B справка уровня 1 (не 4)."""
        user = await make_user(session, "DualB")
        group_a = await services.groups.get_or_create(-720005, "A")
        group_b = await services.groups.get_or_create(-720006, "B")
        await session.commit()
        await services.groups.set_staff(
            group_a.id, user.telegram_id, AdminLevel.OWNER, user.id, 4, user.id
        )
        await services.groups.set_staff(
            group_b.id, user.telegram_id, AdminLevel.OWNER, user.id, 1, user.id
        )
        msg = await _msg(user, "/acmdhelp", chat_id=group_b.telegram_chat_id,
                         chat_type="supergroup")
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session, services=services,
            group=group_b,
        )
        text = msg.answers[0]
        assert "Helper" in text          # уровень B (Lv.1), не A (Lv.4)
        assert "/settings" not in text   # Lv.4-команды группы A не показаны
        assert "/mute" in text           # Lv.1-команды группы B доступны


class TestAcmdhelpAccess:
    """TEST 9, 11, 12, 18: отказы, ЛС, отсутствие записей."""

    async def test_plain_player_denied(self, services, session):
        """TEST 9: обычный игрок — отказ, список не раскрыт."""
        user = await make_user(session, "Noob")
        group = await services.groups.get_or_create(-730001, "G")
        msg = await _msg(user, "/acmdhelp", chat_id=group.telegram_chat_id,
                         chat_type="supergroup")
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session, services=services,
            group=group,
        )
        assert "только администраторам" in msg.answers[0]
        assert "АДМИНИСТРАТИВНЫЕ КОМАНДЫ" not in msg.answers[0]

    async def test_private_plain_player_denied(self, services, session):
        """TEST 11: в ЛС обычный пользователь — отказ (права групп не берутся)."""
        user = await make_user(session, "Lonely")
        group_a = await services.groups.get_or_create(-730002, "A")
        await session.commit()
        await services.groups.set_staff(
            group_a.id, user.telegram_id, AdminLevel.OWNER, user.id, 4, user.id
        )
        msg = await _msg(user, "/acmdhelp")  # ЛС
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session, services=services,
            group=None,
        )
        # локальный уровень группы A в ЛС НЕ действует
        assert "только администраторам" in msg.answers[0]

    async def test_private_global_owner_gets_owner_help(self, services, session, monkeypatch):
        """TEST 12: глобальный Owner в ЛС — глобальная Owner-справка."""
        owner = await make_user(session, "GodPm")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
        msg = await _msg(owner, "/acmdhelp")
        await call_like_aiogram(
            ch.cmd_acmdhelp, message=msg, session=session, services=services,
            group=None,
        )
        text = msg.answers[0]
        assert "глобальный" in text
        assert "/owner" in text and "/admin" in text
        # групповые команды в ЛС скрыты
        assert "/settings" not in text
        assert "/ban" not in text

    async def test_acmdhelp_no_db_writes(self, services, session):
        """TEST 18: повторные /acmdhelp не создают записей."""
        user = await make_user(session, "Clean2")
        group = await services.groups.get_or_create(-730003, "G")
        await session.commit()
        await services.groups.set_staff(
            group.id, user.telegram_id, AdminLevel.OWNER, user.id, 3, user.id
        )
        before = await _db_rows(session)
        for _ in range(3):
            msg = await _msg(user, "/acmdhelp", chat_id=group.telegram_chat_id,
                             chat_type="supergroup")
            await call_like_aiogram(
                ch.cmd_acmdhelp, message=msg, session=session, services=services,
                group=group,
            )
        assert await _db_rows(session) == before


class TestExistingCommandsUntouched:
    """TEST 13–15: /start, /setup, /settings работают как раньше."""

    async def test_start_unchanged(self, services, session, monkeypatch):
        """TEST 13: /start — прежнее главное меню."""
        from bot.handlers import start as st

        user = await make_user(session, "Starter")
        monkeypatch.setattr("bot.config.get_settings", lambda: services.settings)
        msg = await _msg(user, "/start")
        await call_like_aiogram(st.cmd_start, message=msg, db_user=user)
        assert msg.answers and "МАФИЯ ОНЛАЙН" in msg.answers[0]

    async def test_setup_unchanged(self, services, session):
        """TEST 14: /setup — прежняя проверка прав/настройки."""
        from bot.handlers import setup as sp
        from tests.test_setup import FakeSetupBot

        user = await make_user(session, "Plain")
        group = await services.groups.get_or_create(-740001, "S")
        bot = FakeSetupBot(-740001)  # юзер — не админ
        msg = await _msg(user, "/setup", chat_id=group.telegram_chat_id,
                         chat_type="supergroup")
        await call_like_aiogram(
            sp.cmd_setup, message=msg, session=session, services=services,
            db_user=user, group=group, bot=bot,
        )
        assert any("нет прав" in t for t in msg.answers)

    async def test_settings_unchanged(self, services, session):
        """TEST 15: /settings — прежние права MANAGE_SETTINGS."""
        from bot.handlers import groups_admin as ga

        user = await make_user(session, "NoStaff")
        group = await services.groups.get_or_create(-740002, "S")
        msg = await _msg(user, "/settings", chat_id=group.telegram_chat_id,
                         chat_type="supergroup")
        msg.bot = None
        await call_like_aiogram(
            ga.cmd_settings, message=msg, session=session, group=group,
            services=services,
        )
        assert any("Нет права MANAGE_SETTINGS" in t for t in msg.answers)


class TestCallbacksAndRegistry:
    """TEST 16, 19, 20: callback-конфликты, реальность команд, пороги прав."""

    def test_no_callback_conflicts(self):
        """TEST 16: справка не вводит callback_data — конфликтов нет;
        существующие префиксы уникальны."""
        import bot.utils.callbacks as cb
        import bot.utils.command_registry as cr

        # реестр и справка не создают CallbackData-классов
        new_cb = [
            name for name in dir(cr) if name.endswith("CB")
        ]
        assert new_cb == []
        # префиксы существующих CallbackData не дублируются
        prefixes = [getattr(cb, name).__prefix__
                    for name in dir(cb)
                    if isinstance(getattr(cb, name), type)
                    and issubclass(getattr(cb, name), cb.CallbackData)
                    and getattr(cb, name) is not cb.CallbackData]
        assert len(prefixes) == len(set(prefixes)), prefixes
        # SetupCB (кнопка настройки) по-прежнему работает
        assert SetupCB(action="check").pack().startswith("setup:")

    def test_all_registry_commands_exist_in_handlers(self):
        """TEST 19: каждая команда реестра реально зарегистрирована.

        Полный двусторонний инвариант (REAL → REGISTRY и REGISTRY → REAL,
        включая алиасы) живёт в tests/test_command_registry.py; здесь —
        быстрая проверка основного направления через фактическое дерево
        роутеров aiogram (никакого regex-поиска по исходникам).
        """
        from tests.test_command_registry import (
            collect_registered_commands,
            registry_names,
        )

        registered = collect_registered_commands()
        assert not (registry_names() - registered), \
            sorted(registry_names() - registered)

    def test_registry_levels_match_permissions(self):
        """TEST 20: порог уровня команды = минимальный уровень с этим правом
        в реальной LEVEL_PERMISSIONS (справка не выдумывает уровни)."""
        for meta in ADMIN_COMMANDS:
            if meta.permission is None:
                continue
            expected = min(
                lvl for lvl, perms in LEVEL_PERMISSIONS.items()
                if meta.permission in perms
            )
            assert min_level(meta) == int(expected), meta.command

    def test_settings_requires_lv4_really(self):
        """TEST 20 (факт): /settings требует MANAGE_SETTINGS — впервые
        появляется на Lv.4, как и пишет справка."""
        assert min(
            lvl for lvl, perms in LEVEL_PERMISSIONS.items()
            if Permission.MANAGE_SETTINGS in perms
        ) == AdminLevel.SENIOR_ADMIN
        meta = next(m for m in ADMIN_COMMANDS if m.command == "settings")
        assert min_level(meta) == 4

    def test_warn_requires_lv2_really(self):
        meta = next(m for m in ADMIN_COMMANDS if m.command == "warn")
        assert min_level(meta) == 2  # WARN_PLAYER — Moderator+

    def test_maintenance_requires_owner(self):
        meta = next(m for m in ADMIN_COMMANDS if m.command == "maintenance")
        assert min_level(meta) == 5  # MANAGE_GLOBAL_SETTINGS — только Owner


class TestHelpTextRendering:
    """Доп. проверки рендера справки."""

    def test_player_help_categories(self):
        text = player_help_text(in_group=True)
        for title in ("МЕНЮ", "ПРОФИЛЬ", "ДРУЗЬЯ И ИГРОКИ", "ИГРА"):
            assert title in text

    def test_admin_help_hides_global_for_local(self):
        """Локальный Lv.4 не видит глобальную администрацию."""
        text = admin_help_text(level=4, is_global=False, in_group=True)
        assert "/owner" not in text and "/admin" not in text
        assert "/settings" in text

    def test_admin_help_global_lv4_sees_global_panels(self):
        """Глобальный senior admin (ADMIN_IDS) видит глобальные Lv.4-команды."""
        text = admin_help_text(level=4, is_global=True, in_group=False)
        assert "/admin" in text and "/reward_grant" in text
        assert "/owner" not in text  # но не Owner-панель
        assert "/settings" not in text  # групповые в ЛС скрыты

    def test_admin_help_private_hides_group_commands(self):
        text = admin_help_text(level=5, is_global=True, in_group=False)
        assert "/owner" in text and "/set_rating" in text
        assert "/ban" not in text and "/mute" not in text
