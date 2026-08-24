"""Конфигурация приложения на Pydantic Settings.

Все секреты читаются из переменных окружения / файла .env.
Файл .env в git не попадает (см. .gitignore).
"""

from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Загружаем .env в окружение до создания Settings.
load_dotenv()


class Settings(BaseSettings):
    """Глобальные настройки бота."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---------------------------------------------------------
    bot_token: str = Field(validation_alias="BOT_TOKEN")
    # ADMIN_IDS приходит строкой «111,222»: pydantic-settings ожидает JSON
    # для list-полей, поэтому храним строку, а список отдаём свойством.
    admin_ids: str = Field(default="", validation_alias="ADMIN_IDS")
    # Глобальные Owner'ы (уровень 5 во всех чатах, см. PermissionService)
    owner_ids: str = Field(default="", validation_alias="OWNER_IDS")

    # --- База данных --------------------------------------------------------
    # SQLite — для разработки; PostgreSQL подставляется одной строкой в .env:
    #   postgresql+asyncpg://user:pass@localhost:5432/mafia
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/mafia.db",
        validation_alias="DATABASE_URL",
    )
    auto_create_tables: bool = Field(default=True, validation_alias="AUTO_CREATE_TABLES")

    # --- Логирование --------------------------------------------------------
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_file: str = Field(default="logs/bot.log", validation_alias="LOG_FILE")

    # --- DEBUG MODE ---------------------------------------------------------
    # Включает /testgame (создание тестовых игр с ботами). Доступно только ADMIN_IDS.
    debug_mode: bool = Field(default=False, validation_alias="DEBUG_MODE")
    # По умолчанию тестовые игры НЕ меняют настоящую статистику.
    debug_affects_global_stats: bool = Field(default=False, validation_alias="DEBUG_AFFECTS_GLOBAL_STATS")
    debug_affects_local_stats: bool = Field(default=False, validation_alias="DEBUG_AFFECTS_LOCAL_STATS")

    # --- Игровые значения по умолчанию (переопределяются настройками комнаты)
    default_night_seconds: int = Field(default=90, validation_alias="DEFAULT_NIGHT_SECONDS")
    default_day_seconds: int = Field(default=180, validation_alias="DEFAULT_DAY_SECONDS")
    default_vote_seconds: int = Field(default=60, validation_alias="DEFAULT_VOTE_SECONDS")
    start_countdown_seconds: int = Field(default=5, validation_alias="START_COUNTDOWN_SECONDS")
    min_players_limit: int = Field(default=4, validation_alias="MIN_PLAYERS_LIMIT")
    max_players_limit: int = Field(default=20, validation_alias="MAX_PLAYERS_LIMIT")

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        if not value or ":" not in value:
            raise ValueError(
                "BOT_TOKEN не задан или некорректен. Скопируйте .env.example -> .env "
                "и укажите токен от @BotFather."
            )
        return value

    def admin_id_list(self) -> list[int]:
        """«123,456» -> [123, 456]."""
        return self._parse_ids(self.admin_ids)

    def owner_id_list(self) -> list[int]:
        return self._parse_ids(self.owner_ids)

    @staticmethod
    def _parse_ids(raw: str) -> list[int]:
        if not raw:
            return []
        return [int(p.strip()) for p in raw.replace(";", ",").split(",") if p.strip()]

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_id_list()

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.owner_id_list()

    def sqlite_mode(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек (lru_cache — чтобы .env читался один раз)."""
    return Settings()


def reset_settings_cache() -> None:
    """Используется в тестах для пересоздания настроек."""
    get_settings.cache_clear()
