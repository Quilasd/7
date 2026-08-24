"""UserLookupService: поиск пользователя для админ-команд.

Поддерживает:
- Telegram ID:        /profile 123456789
- @username:          /profile @StalkerGamer
- username без @:     /profile StalkerGamer
- reply/mention:      /profile (ответом на сообщение пользователя)

Основной идентификатор — telegram_id; username — вспомогательный способ.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import User
from bot.database.repositories.users import UserRepository


class UserLookupService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def resolve(
        self,
        query: str | None = None,
        reply_telegram_id: int | None = None,
    ) -> User | None:
        """Сначала query (ID/username), затем reply-контекст."""
        if query:
            user = await self._by_query(query.strip())
            if user is not None:
                return user
        if reply_telegram_id:
            return await self.users.get_by_telegram_id(reply_telegram_id)
        return None

    async def _by_query(self, raw: str) -> User | None:
        # Убираем угловые скобки из упоминаний вида <a href="tg://user?id=1">
        token = raw.strip().strip("<>")
        if token.lstrip("-").isdigit():
            return await self.users.get_by_telegram_id(int(token))
        username = token.lstrip("@").lower()
        if not username:
            return None
        result = await self.session.execute(
            select(User).where(
                # SQLite регистрозависим для не-ASCII, но username в Telegram всегда латиница
                User.username == username
            )
        )
        return result.scalars().unique().one_or_none()
