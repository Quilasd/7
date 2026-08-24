"""Подсказки команд Telegram (set_my_commands): scope по ролям.

Проверяется UX-слой (какие списки кому показываются); серверные проверки
прав НЕ зависят от подсказок и остаются в PermissionService.
"""

from __future__ import annotations

from aiogram.types import BotCommand, BotCommandScopeChat

from bot.utils.commands_menu import (
    ADMIN_COMMANDS,
    GROUP_COMMANDS,
    OWNER_COMMANDS,
    USER_COMMANDS,
    commands_for,
    setup_bot_commands,
)
from tests.conftest import SettingsStub


class FakeMenuBot:
    """Записывает вызовы set_my_commands (scope, команды)."""

    def __init__(self, fail_chat_ids: set[int] | None = None) -> None:
        self.calls: list[tuple[object, tuple[str, ...]]] = []
        self.fail_chat_ids = fail_chat_ids or set()

    async def set_my_commands(self, commands, scope=None) -> bool:
        from aiogram.exceptions import TelegramAPIError

        if isinstance(scope, BotCommandScopeChat) and scope.chat_id in self.fail_chat_ids:
            raise TelegramAPIError(method="setMyCommands", message="chat not found")
        self.calls.append((scope, tuple(c.command for c in commands)))
        return True


def _names(commands) -> set[str]:
    return {c.command for c in commands}


class TestCommandLists:
    def test_user_commands_have_no_admin_entries(self):
        assert "admin" not in _names(USER_COMMANDS)
        assert "broadcast" not in _names(USER_COMMANDS)
        assert "testgame" not in _names(USER_COMMANDS)
        assert "debug_help" not in _names(USER_COMMANDS)
        assert "maintenance" not in _names(USER_COMMANDS)

    def test_all_bot_commands_valid(self):
        # aiogram требует: 1-32 символа, [a-z0-9_], описание 1-256
        for command in USER_COMMANDS + ADMIN_COMMANDS + OWNER_COMMANDS + GROUP_COMMANDS:
            assert isinstance(command, BotCommand)
            assert 1 <= len(command.command) <= 32
            assert command.command.islower()
            assert command.description.strip()

    def test_commands_for_roles(self):
        plain = _names(commands_for())
        admin = _names(commands_for(is_admin=True))
        owner = _names(commands_for(is_owner=True))

        assert "profile" in plain and "admin" not in plain
        assert "admin" in admin and "debug_help" not in admin
        assert "debug_help" in owner and "broadcast" in owner
        # нарастающие множества
        assert plain < admin < owner


class TestSetupBotCommands:
    async def test_scopes_and_personal_lists(self):
        bot = FakeMenuBot()
        settings = SettingsStub()
        settings._owners = [111]
        settings._admins = [111, 222]  # 111 и там и там -> считается владельцем

        await setup_bot_commands(bot, settings)

        scopes = {type(scope).__name__: names for scope, names in bot.calls}
        # базовый список для всех + короткий для групп
        assert "BotCommandScopeDefault" in scopes
        assert "BotCommandScopeAllGroupChats" in scopes
        assert "profile" in scopes["BotCommandScopeDefault"]
        assert "admin" not in scopes["BotCommandScopeDefault"]
        assert "broadcast" not in scopes["BotCommandScopeAllGroupChats"]

        # персональные: владелец 111 (admin+owner), админ 222 (только admin)
        personal = {scope.chat_id: names for scope, names in bot.calls
                    if isinstance(scope, BotCommandScopeChat)}
        assert set(personal) == {111, 222}
        assert "debug_help" in personal[111]
        assert "broadcast" in personal[111]
        assert "broadcast" in personal[222]
        assert "debug_help" not in personal[222]

    async def test_failed_personal_scope_does_not_break_setup(self):
        # админ ещё не начинал диалог -> Telegram отклоняет scope чата
        bot = FakeMenuBot(fail_chat_ids={333})
        settings = SettingsStub()
        settings._admins = [333]

        await setup_bot_commands(bot, settings)  # не должно бросать

        scope_types = [type(scope).__name__ for scope, _ in bot.calls]
        assert "BotCommandScopeDefault" in scope_types  # база всё равно установлена

    async def test_no_admins_means_no_personal_scopes(self):
        bot = FakeMenuBot()
        await setup_bot_commands(bot, SettingsStub())
        assert not any(
            isinstance(scope, BotCommandScopeChat) for scope, _ in bot.calls
        )
