"""AuditService: журнал административных действий."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import AuditLog
from bot.database.repositories.groups import AuditLogRepository

logger = logging.getLogger(__name__)


class AuditService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def log(
        self,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        group_id: int | None = None,
        details: str = "",
    ) -> None:
        async with self.session_factory() as session:
            await AuditLogRepository(session).log(
                actor_id=actor_user_id,
                action=action,
                target_id=target_user_id,
                group_id=group_id,
                details=details,
            )
            await session.commit()
        logger.info(
            "АУДИТ: actor=%s action=%s target=%s group=%s %s",
            actor_user_id, action, target_user_id, group_id, details,
        )

    async def last(self, group_id: int | None = None, limit: int = 20) -> list[AuditLog]:
        async with self.session_factory() as session:
            return await AuditLogRepository(session).last(group_id, limit)
