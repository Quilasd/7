"""DI: прокидывает контейнер сервисов в хендлеры (data['services'])."""

from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, services) -> None:
        self.services = services

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["services"] = self.services
        return await handler(event, data)
