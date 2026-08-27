"""Серверная модерация игровых чатов (Game Chat / Mafia Chat).

Message-middleware: любое сообщение в чате, привязанном к АКТИВНОЙ игре,
проходит проверку на сервере (не полагаемся только на Telegram permissions):
- общий игровой чат: писать могут только ЖИВЫЕ участники и только днём
  (DAY/VOTING); ночью и мёртвым — сообщение удаляется;
- чат мафии: писать могут только ЖИВЫЕ мафиози и только ночью;
- команды привязки (/gamechat, /mafiachat) пропускаются к хендлерам.

Непривязанные чаты (обычные группы) middleware не трогает.
"""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware

logger = logging.getLogger(__name__)

LINK_COMMANDS = ("/gamechat", "/mafiachat")


class GameChatGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: dict):
        services = data.get("services")
        game_chats = getattr(services, "game_chats", None)
        chat = getattr(event, "chat", None)
        if (
            game_chats is None
            or chat is None
            or chat.type not in ("group", "supergroup")
        ):
            return await handler(event, data)

        text = event.text or ""
        if text.startswith(LINK_COMMANDS):
            return await handler(event, data)  # команды привязки — хендлеру

        session = data.get("session")
        db_user = data.get("db_user")
        if session is None or db_user is None:
            return await handler(event, data)

        try:
            handled = await game_chats.enforce_message(
                session, event.chat.id, db_user, event.message_id,
                is_command=text.startswith("/"),
            )
        except Exception:
            logger.warning("GameChat: сбой модерации сообщения", exc_info=True)
            handled = False
        if handled:
            return None  # сообщение удалено — к хендлерам не идёт
        return await handler(event, data)
