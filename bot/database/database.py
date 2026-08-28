"""Инициализация async-engine и сессий.

Для разработки — SQLite (aiosqlite), для продакшена достаточно заменить
DATABASE_URL на postgresql+asyncpg://... — весь код работает через async
SQLAlchemy и от диалекта не зависит.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.database.models import Base

logger = logging.getLogger(__name__)


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


async def sync_derived_levels(engine: AsyncEngine) -> int:
    """Приводит сохранённый ``level`` в соответствие с XP (``level = f(xp)``).

    Поле level — ПРОИЗВОДНОЕ от xp (инвариант зафиксирован миграцией 0006):
    все write-пути (RatingService, owner/admin set_xp/set_level) пересчитывают
    его сами, но при смене кривой XP существующие строки БД пересчитывает
    только alembic-миграция 0006 — а при обновлении через create_all
    (AUTO_CREATE_TABLES=true, без alembic) она не выполняется, и сохранённый
    уровень ОТСТАЁТ от новой кривой. Тогда /profile (уровень выводится из XP)
    и рейтинги (/top*, /owner — сохранённый level) показывают РАЗНЫЕ уровни
    одного и того же игрока.

    Идемпотентна: обновляются только расходящиеся строки; кривая берётся из
    единого источника (ProgressionService), копии формулы нет. Возвращает
    количество исправленных строк.
    """
    from sqlalchemy import text

    from bot.services.progression import DEFAULT_PROGRESSION

    updated = 0
    async with engine.begin() as conn:
        for table in ("users", "group_players"):
            try:
                rows = (await conn.execute(
                    text(f"SELECT id, xp, level FROM {table}")
                )).fetchall()
            except Exception:  # таблицы нет (БД ведёт alembic) — не страшно
                logger.warning("sync_derived_levels: таблица %s недоступна", table)
                continue
            for row_id, xp, level in rows:
                new_level = DEFAULT_PROGRESSION.level_for_xp(int(xp or 0))
                if int(level or 1) != new_level:
                    await conn.execute(
                        text(f"UPDATE {table} SET level = :lvl WHERE id = :pid"),
                        {"lvl": new_level, "pid": row_id},
                    )
                    updated += 1
    if updated:
        logger.info("Уровни пересинхронизированы с кривой XP: строк исправлено %s", updated)
    return updated


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
