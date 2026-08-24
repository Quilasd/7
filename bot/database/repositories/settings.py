"""Репозиторий глобальных настроек приложения (app_settings)."""

from __future__ import annotations


from bot.database.models import AppSetting
from bot.database.repositories.base import BaseRepository

GLOBAL_KEY = "global"


class AppSettingRepository(BaseRepository[AppSetting]):
    model = AppSetting

    async def get_global(self) -> dict:
        obj = await self.session.get(AppSetting, GLOBAL_KEY)
        return dict(obj.value) if obj else {}

    async def set_global(self, value: dict) -> None:
        obj = await self.session.get(AppSetting, GLOBAL_KEY)
        if obj is None:
            obj = AppSetting(key=GLOBAL_KEY, value=value)
            self.session.add(obj)
        else:
            obj.value = value
        await self.session.flush()
