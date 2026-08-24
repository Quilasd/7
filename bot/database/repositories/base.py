"""Базовый репозиторий: общие CRUD-помощники поверх AsyncSession."""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[Base]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: int) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def add(self, obj: Base) -> Base:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(self.model))
        return int(result.scalar_one())
