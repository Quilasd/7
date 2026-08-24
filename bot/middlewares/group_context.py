"""Контекст группы и режим обслуживания.

GroupContextMiddleware (сообщения и колбэки):
- в group/supergroup: находит/создает Group, GroupPlayer и GroupSettings;
  кладёт в data["group"], data["group_settings"];
- в private chat: data["group"] = None (глобальный контекст).

MaintenanceMiddleware: если в app_settings включён maintenance, не-админы
получают вежливый отказ (глобальный владелец/админ проходит всегда).
Флаг кэшируется в памяти на CACHE_TTL секунд, /reload сбрасывает кэш.
"""

from __future__ import annotations

import logging
import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.repositories.groups import (
    GroupPlayerRepository,
    GroupRepository,
    GroupSettingsRepository,
)

logger = logging.getLogger(__name__)


class GroupContextMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        session = data.get("session")
        chat = data.get("event_chat")
        group = None
        settings = None
        if session is not None and chat is not None and chat.type in ("group", "supergroup"):
            groups = GroupRepository(session)
            group = await groups.get_or_create(chat.id, chat.title or "")
            settings = await GroupSettingsRepository(session).get_or_create(group.id)
            user = data.get("db_user")
            if user is not None and not getattr(user, "is_test", False):
                await GroupPlayerRepository(session).ensure(group.id, user.id)
            await session.commit()
        data["group"] = group
        data["group_settings"] = settings
        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    CACHE_TTL = 15.0

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self._enabled: bool | None = None
        self._checked_at: float = 0.0

    async def _maintenance_enabled(self) -> bool:
        now = time.monotonic()
        if self._enabled is None or now - self._checked_at > self.CACHE_TTL:
            from bot.database.repositories.settings import AppSettingRepository

            try:
                async with self.session_factory() as session:
                    stored = await AppSettingRepository(session).get_global()
                self._enabled = bool(stored.get("maintenance", False))
            except Exception:  # pragma: no cover - БД недоступна
                self._enabled = False
            self._checked_at = now
        return self._enabled

    def invalidate(self) -> None:
        self._enabled = None
        self._checked_at = 0.0

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not await self._maintenance_enabled():
            return await handler(event, data)

        from bot.config import get_settings

        user = data.get("event_from_user")
        settings = get_settings()
        if user is not None and (
            settings.is_admin(user.id) or settings.is_owner(user.id)
        ):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("🛠 Бот на техническом обслуживании. Скоро вернёмся!", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("🛠 Бот на техническом обслуживании. Скоро вернёмся!")
        return None
