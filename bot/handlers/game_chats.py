"""Настройка игровых форумов (Game Forum / Mafia Forum).

Темы партии создаются БОТОМ автоматически при старте игры в двух постоянных
форумных чатах (GAME_FORUM_CHAT_ID / MAFIA_FORUM_CHAT_ID) — ручных команд
на каждую игру больше не нужно (/gamechat и /mafiachat удалены).

Здесь только OWNER-настройка постоянных форумов:
- /set_game_forum  [chat_id] — выполнить в самом форуме или с аргументом;
- /set_mafia_forum [chat_id] — аналогично для форума мафии.

Модерация тем партий — GameChatGuardMiddleware (bot/middlewares).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

router = Router(name="game_forums")


async def _set_forum(message: Message, session, services, db_user, kind: str) -> None:
    settings = services.settings
    is_owner = db_user is not None and settings is not None and (
        db_user.telegram_id in (getattr(settings, "_owners", None) or [])
    )
    if not is_owner:
        await message.answer("⚙️ Настройку форумов может выполнять только владелец бота.")
        return

    # chat_id: аргумент команды либо чат, в котором она выполнена
    parts = (message.text or "").split()
    chat_id = None
    if len(parts) > 1 and parts[1].lstrip("-").isdigit():
        chat_id = int(parts[1])
    elif message.chat.type in ("group", "supergroup"):
        chat_id = message.chat.id
    if chat_id is None:
        await message.answer(
            f"Формат: <code>/{'set_game_forum' if kind == 'game' else 'set_mafia_forum'} "
            "&lt;chat_id&gt;</code> — или выполните команду в самом форумном чате."
        )
        return

    app_config = getattr(services, "app_config", None)
    if app_config is None:
        await message.answer("Конфигурация недоступна.")
        return
    gs = await app_config.get()
    if kind == "game":
        gs.game_forum_chat_id = chat_id
    else:
        gs.mafia_forum_chat_id = chat_id
    await app_config.save(gs)

    # сразу проверяем доступ
    forums = await services.game_chats.check_forums()
    info = forums[kind]
    status = "✅ форум доступен" if info["ok"] else f"⚠️ {info.get('error', 'проблема')}"
    label = "Game Forum" if kind == "game" else "Mafia Forum"
    await message.answer(
        f"⚙️ {label} настроен: <code>{chat_id}</code>\n{status}\n\n"
        + ("" if info["ok"] else
           "Убедитесь: это супергруппа-форум (включены темы) и бот — администратор "
           "с правом <b>can_manage_topics</b>.")
    )
    logger.info("Настроен %s: %s (user %s)", label, chat_id, db_user.id)


@router.message(Command("set_game_forum"))
async def cmd_set_game_forum(message: Message, session, services, db_user) -> None:
    await _set_forum(message, session, services, db_user, "game")


@router.message(Command("set_mafia_forum"))
async def cmd_set_mafia_forum(message: Message, session, services, db_user) -> None:
    await _set_forum(message, session, services, db_user, "mafia")
