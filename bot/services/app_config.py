"""Глобальные настройки приложения (хранятся в БД, редактируются админом)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bot.config import Settings
from bot.database.repositories.settings import AppSettingRepository
from bot.roles import all_roles


class GlobalSettings(BaseModel):
    enabled_roles: list[str] = Field(default_factory=list)  # пусто = все роли
    night_seconds: int = 90
    day_seconds: int = 180
    vote_seconds: int = 60
    # Форумные чаты партий (None = не задано в БД -> берётся из .env)
    game_forum_chat_id: int | None = None
    mafia_forum_chat_id: int | None = None

    def is_role_enabled(self, role_id: str) -> bool:
        return not self.enabled_roles or role_id in self.enabled_roles

    def enabled_role_objects(self):
        return [r for r in all_roles() if self.is_role_enabled(r.id) and r.id != "citizen"]


class AppConfigService:
    """Глобальные настройки поверх дефолтов из .env."""

    def __init__(self, session_factory, settings: Settings) -> None:
        self.session_factory = session_factory
        self.env_settings = settings

    async def get(self) -> GlobalSettings:
        defaults = GlobalSettings(
            night_seconds=self.env_settings.default_night_seconds,
            day_seconds=self.env_settings.default_day_seconds,
            vote_seconds=self.env_settings.default_vote_seconds,
            game_forum_chat_id=getattr(self.env_settings, "game_forum_chat_id", None),
            mafia_forum_chat_id=getattr(self.env_settings, "mafia_forum_chat_id", None),
        )
        async with self.session_factory() as session:
            repo = AppSettingRepository(session)
            stored = await repo.get_global()
        if not stored:
            return defaults
        return GlobalSettings(**{**defaults.model_dump(), **stored})

    async def save(self, gs: GlobalSettings) -> None:
        async with self.session_factory() as session:
            repo = AppSettingRepository(session)
            await repo.set_global(gs.model_dump())
            await session.commit()
