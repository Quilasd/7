"""Автоподсказки команд Telegram (меню «/»).

set_my_commands с разными scope:
- BotCommandScopeDefault       — базовый список для всех;
- BotCommandScopeAllGroupChats — короткий список для групп;
- BotCommandScopeChat(chat_id=админ) — расширенный список лично для
  глобальной администрации из .env (OWNER_IDS + ADMIN_IDS);
- BotCommandScopeChatMember(chat_id, user_id) — персональный список для
  КОНКРЕТНОГО участника группы (локальному админу после /claim показываем
  админ-команды этой группы; в группе иначе виден только общий group-scope).

ВАЖНО: подсказки — это только UX. Скрытие команды из меню НЕ является
защитой: все права по-прежнему проверяются на сервере через
PermissionService (см. bot/services/permissions.py) при каждом вызове.

Админские scope выставляются при старте бота; если админ ещё не начал
диалог с ботом, Telegram отклонит персональный scope — это не ошибка,
пишем warning и продолжаем (при первом /start можно вызвать refresh).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
)

from bot.config import Settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ списки

#: Команды для всех пользователей (личные чаты, scope по умолчанию)
USER_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="🧭 Главное меню"),
    BotCommand(command="profile", description="👤 Профиль: 🌐 глобально + 🏠 группа"),
    BotCommand(command="stats", description="📊 Моя статистика"),
    BotCommand(command="top", description="🏆 Рейтинги: 🌐 глобальный / 🏠 группы"),
    BotCommand(command="group_stats", description="🏠 Статистика этой группы"),
    BotCommand(command="global_stats", description="🌐 Глобальная статистика"),
    BotCommand(command="cancel", description="❌ Отмена текущего действия"),
    BotCommand(command="help", description="❓ Помощь"),
]

#: Короткий список для групповых чатов (в группе меню «/» тоже доступно)
GROUP_COMMANDS: list[BotCommand] = [
    BotCommand(command="top", description="🏆 Рейтинг этой группы"),
    BotCommand(command="stats", description="📊 Моя статистика"),
    BotCommand(command="group_stats", description="🏠 Статистика группы"),
    BotCommand(command="claim", description="👑 Создателю: забрать права админа"),
    BotCommand(command="cancel", description="❌ Отмена"),
]

#: Доп. команды управления группой — показываются ЛОКАЛЬНОМУ админу группы
#: (получившему уровень через /claim или /staff_add) через scope ChatMember.
GROUP_ADMIN_COMMANDS: list[BotCommand] = [
    BotCommand(command="settings", description="⚙️ Настройки группы"),
    BotCommand(command="staff", description="👥 Штаб группы"),
    BotCommand(command="staff_add", description="➕ Назначить админа: ID 3"),
    BotCommand(command="staff_remove", description="➖ Снять админа"),
    BotCommand(command="staff_info", description="ℹ️ Уровень игрока"),
    BotCommand(command="players", description="👥 Игроки группы"),
    BotCommand(command="createroom", description="➕ Комната с правилами группы"),
    BotCommand(command="room_force_start", description="▶️ Старт комнаты по ID"),
    BotCommand(command="game_stop", description="🛑 Остановить игру"),
]

#: Админские команды (глобальная администрация: OWNER_IDS + ADMIN_IDS)
ADMIN_COMMANDS: list[BotCommand] = [
    BotCommand(command="admin", description="🛠 Админ-панель"),
    BotCommand(command="player", description="👤 Профиль игрока (ID/@username/reply)"),
    BotCommand(command="players", description="👥 Игроки группы"),
    BotCommand(command="warn", description="⚠️ Выдать предупреждение (reply/ID)"),
    BotCommand(command="unwarn", description="✅ Снять предупреждение"),
    BotCommand(command="warnings", description="⚠️ Число предупреждений игрока"),
    BotCommand(command="mute", description="🔇 Мут: /mute 60 (reply/ID)"),
    BotCommand(command="unmute", description="🔊 Снять мут"),
    BotCommand(command="kick", description="👢 Кикнуть из группы"),
    BotCommand(command="ban", description="🚫 Забанить (глобально/локально)"),
    BotCommand(command="unban", description="✅ Разбанить"),
    BotCommand(command="game", description="🎮 Активная игра группы"),
    BotCommand(command="games", description="🎮 Список игр группы"),
    BotCommand(command="game_stop", description="🛑 Остановить игру по ID"),
    BotCommand(command="rooms", description="🏠 Комнаты группы"),
    BotCommand(command="createroom", description="➕ Комната с правилами группы"),
    BotCommand(command="room_force_start", description="▶️ Старт комнаты по ID"),
    BotCommand(command="staff", description="👥 Штаб группы"),
    BotCommand(command="staff_add", description="➕ Назначить админа: /staff_add ID 3"),
    BotCommand(command="staff_remove", description="➖ Снять админа"),
    BotCommand(command="staff_info", description="ℹ️ Уровень игрока"),
    BotCommand(command="settings", description="⚙️ Настройки группы"),
    BotCommand(command="set_roles", description="🎭 Роли: /set_roles mafia 2"),
    BotCommand(command="claim", description="👑 Создателю: забрать права админа"),
    BotCommand(command="broadcast", description="📣 Рассылка"),
    BotCommand(command="botstats", description="📊 Статистика бота"),
    BotCommand(command="logs", description="📜 Логи"),
    BotCommand(command="reload", description="♻️ Сбросить кэш настроек"),
    BotCommand(command="maintenance", description="🛠 Режим обслуживания"),
    BotCommand(command="testgame", description="🧪 Тест-игра с ботами (/testgame fast)"),
    BotCommand(command="debug", description="🐛 DEBUG: статус тест-игры"),
]

#: Только глобальному Owner (OWNER_IDS)
OWNER_COMMANDS: list[BotCommand] = [
    BotCommand(command="debug_help", description="👑 Справочник всех команд владельца"),
]


# --------------------------------------------------------------- публичное

def commands_for(is_admin: bool = False, is_owner: bool = False) -> list[BotCommand]:
    """Итоговый список команд для пользователя по его глобальной роли."""
    commands = list(USER_COMMANDS)
    if is_admin or is_owner:
        commands += ADMIN_COMMANDS
    if is_owner:
        commands += OWNER_COMMANDS
    return commands


async def setup_bot_commands(bot: Bot, settings: Settings) -> None:
    """Регистрирует подсказки команд во всех scope. Вызывается при старте.

    Ошибка установки персонального scope (админ не начал диалог с ботом)
    не должна ронять запуск — пишем warning и продолжаем.
    """
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    except TelegramAPIError as exc:  # pragma: no cover - сетевой сбой
        logger.error("Не удалось установить базовые подсказки команд: %s", exc)
        return

    owners = set(settings.owner_id_list())
    admins = set(settings.admin_id_list()) - owners
    for telegram_id in sorted(owners | admins):
        scope = BotCommandScopeChat(chat_id=telegram_id)
        try:
            await bot.set_my_commands(
                commands_for(
                    is_admin=telegram_id in admins,
                    is_owner=telegram_id in owners,
                ),
                scope=scope,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Подсказки для %s не установлены (напиши боту /start и они появятся): %s",
                telegram_id,
                exc,
            )
    logger.info(
        "Подсказки команд зарегистрированы (администраторов: %s, владельцев: %s)",
        len(admins),
        len(owners),
    )


async def refresh_user_commands(bot: Bot, telegram_id: int, is_admin: bool, is_owner: bool) -> None:
    """Обновляет персональный scope одного пользователя.

    Пригодится, если права выдаются в рантайме (например, локальный штат
    группы решит показывать команды) — сейчас используется для повторного
    применения после первого диалога админа с ботом.
    """
    try:
        await bot.set_my_commands(
            commands_for(is_admin=is_admin, is_owner=is_owner),
            scope=BotCommandScopeChat(chat_id=telegram_id),
        )
    except TelegramAPIError as exc:  # pragma: no cover
        logger.warning("Подсказки для %s не обновлены: %s", telegram_id, exc)


async def set_member_commands(
    bot: Bot, chat_id: int, user_id: int, is_group_admin: bool
) -> None:
    """Команды конкретного участника в группе (scope ChatMember).

    В группе Telegram показывает всем общий group-scope; чтобы локальный
    админ (получивший уровень через /claim или /staff_add) увидел админ-
    команды, выставляем ему персональный набор через BotCommandScopeChatMember.
    Срабатывает только для этого пользователя в этом чате.
    """
    commands = GROUP_COMMANDS + GROUP_ADMIN_COMMANDS if is_group_admin else GROUP_COMMANDS
    try:
        await bot.set_my_commands(
            commands,
            scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=user_id),
        )
    except TelegramAPIError as exc:  # pragma: no cover
        logger.warning(
            "Подсказки участника %s в %s не установлены: %s", user_id, chat_id, exc
        )
