"""Серверная модерация форумных тем партий (Game Topic / Mafia Topic).

Message-middleware: любое сообщение в теме, привязанной к игре, проходит
проверку на СЕРВЕРЕ (per-topic прав у Telegram API нет — restrictChatMember
действует на весь чат, поэтому изоляция обеспечивается удалением сообщений):
- тема игры: писать могут только ЖИВЫЕ участники этой партии и только днём
  (DAY/VOTING); ночью и мёртвым — сообщение удаляется;
- тема мафии: писать могут только ЖИВЫЕ мафиози этой партии и только ночью;
- темы завершённых игр — только чтение (история);
- сообщения вне тем партий (общие темы форумов и обычные группы) не трогаются.

Контекст определяется парой (chat_id, message_thread_id) — уникальной для
каждой игры: параллельные партии в одном форуме не смешиваются.
"""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware

logger = logging.getLogger(__name__)


class GameChatGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data: dict):
        services = data.get("services")
        game_chats = getattr(services, "game_chats", None)
        chat = getattr(event, "chat", None)
        if game_chats is None or chat is None or chat.type != "supergroup":
            return await handler(event, data)

        session = data.get("session")
        db_user = data.get("db_user")
        if session is None or db_user is None:
            return await handler(event, data)

        thread_id = getattr(event, "message_thread_id", None)
        try:
            found = await game_chats.context_for(session, chat.id, thread_id)
        except Exception:
            logger.warning("GameChat: сбой определения темы", exc_info=True)
            found = None
        if found is None:
            return await handler(event, data)  # не тема партии — обычная обработка

        try:
            handled = await game_chats.enforce_message(
                session, chat.id, thread_id, db_user, event.message_id,
                is_command=(event.text or "").startswith("/"),
            )
        except Exception:
            logger.warning("GameChat: сбой модерации сообщения", exc_info=True)
            handled = False
        if handled:
            return None  # сообщение удалено — к хендлерам не идёт
        return await handler(event, data)
