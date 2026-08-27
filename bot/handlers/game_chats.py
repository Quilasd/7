"""Привязка игровых чатов партии (Game Chat / Mafia Chat).

Telegram Bot API не позволяет боту создавать группы и добавлять участников,
поэтому чаты создаёт создатель партии, добавляет бота администратором и
привязывает командами в самих чатах:

- /gamechat <game_id>  — в общем чате партии (🎮 обсуждения днём);
- /mafiachat <game_id> — в чате мафии (🌙 только живая мафия ночью).

После привязки бот: ставит title, шлёт игрокам инвайт-ссылки в ЛС (вступают
сами — боты не могут добавлять участников), синхронизирует права по текущей
фазе, ведёт анонсы и модерацию (мёртвые/неучастники/ночь — молчание).

Права на привязку: создатель партии или глобальный владелец/админ
(серверная проверка в GameChatService и здесь).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.database.repositories.games import GameRepository

logger = logging.getLogger(__name__)

router = Router(name="game_chats")


async def _link(message: Message, session, services, db_user, kind: str) -> None:
    parts = (message.text or "").split()
    cmd = "gamechat" if kind == "game" else "mafiachat"
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            f"Формат: <code>/{cmd} &lt;ID игры&gt;</code>\n"
            f"Например: <code>/{cmd} 123</code>"
        )
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду нужно выполнить в привязываемом групповом чате.")
        return

    game_id = int(parts[1])
    game = await GameRepository(session).get(game_id)
    if game is None:
        await message.answer(f"Игра #{game_id} не найдена.")
        return

    settings = services.settings
    is_privileged = db_user is not None and settings is not None and (
        db_user.telegram_id in (getattr(settings, "_owners", None) or [])
        or db_user.telegram_id in (getattr(settings, "_admins", None) or [])
    )
    ok, text = await services.game_chats.link_chat(
        session, game, message.chat.id, kind, db_user.id,
        is_privileged=is_privileged,
    )
    await session.commit()
    await message.answer(text)
    if ok:
        logger.info(
            "Chat %s привязан к игре %s (%s) пользователем %s",
            message.chat.id, game_id, kind, db_user.id,
        )


@router.message(Command("gamechat"))
async def cmd_gamechat(message: Message, session, services, db_user) -> None:
    """Привязать этот чат как общий игровой чат партии."""
    await _link(message, session, services, db_user, "game")


@router.message(Command("mafiachat"))
async def cmd_mafiachat(message: Message, session, services, db_user) -> None:
    """Привязать этот чат как ночной чат мафии партии."""
    await _link(message, session, services, db_user, "mafia")
