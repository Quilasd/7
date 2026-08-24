"""UserMiddleware: upsert пользователя + проверка бана."""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as AiogramUser

from bot.database.repositories.users import UserRepository

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg_user: AiogramUser | None = data.get("event_from_user")
        session = data.get("session")
        if tg_user is None or session is None:
            return await handler(event, data)
        if tg_user.is_bot:
            return None

        repo = UserRepository(session)
        user = await repo.upsert_from_telegram(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
        )
        await session.commit()

        if user.is_banned:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Вы заблокированы в этом боте.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🚫 Вы заблокированы в этом боте.")
            return None  # не пропускаем обработку дальше

        data["db_user"] = user
        return await handler(event, data)
