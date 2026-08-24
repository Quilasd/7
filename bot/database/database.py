"""Инициализация async-engine и сессий.

Для разработки — SQLite (aiosqlite), для продакшена достаточно заменить
DATABASE_URL на postgresql+asyncpg://... — весь код работает через async
SQLAlchemy и от диалекта не зависит.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.database.models import Base

logger = logging.getLogger(__name__)


def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        # Каталог для файла БД создаём заранее
        path = database_url.split("///")[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
        # Включаем внешние ключи в SQLite (по умолчанию они выключены)
        @event.listens_for(Engine, "connect")
        def _sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return create_async_engine(database_url, echo=False, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт таблицы, если их нет (удобно для dev/тестов).

    В продакшене рекомендуется alembic (см. README, раздел «Миграции»).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Таблицы БД готовы")


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
