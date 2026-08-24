"""Простой антиспам: не чаще одного действия в `rate` секунд на пользователя."""

from __future__ import annotations

import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate: float = 0.35) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро", cache_time=1)
                return None
            self._last[user.id] = now
            # не раздуваем память
            if len(self._last) > 10_000:
                cutoff = now - 60
                self._last = {uid: t for uid, t in self._last.items() if t > cutoff}
        return await handler(event, data)
