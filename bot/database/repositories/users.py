"""Репозиторий пользователей."""

from __future__ import annotations

from sqlalchemy import func, select, update

from bot.database.models import User
from bot.database.repositories.base import BaseRepository
from bot.utils.helpers import utcnow


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.session.get(User, user_id)

    async def upsert_from_telegram(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> User:
        """Создаёт или обновляет пользователя при каждом апдейте."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            user = User(
                telegram_id=telegram_id,
                username=username,
                display_name=(first_name or "").strip()[:64],
            )
            self.session.add(user)
            await self.session.flush()
        else:
            user.username = username
            if not user.display_name and first_name:
                user.display_name = first_name.strip()[:64]
            user.last_seen_at = utcnow()
            await self.session.flush()
        return user

    async def top_by_rating(self, limit: int = 10) -> list[User]:
        result = await self.session.execute(
            select(User)
            .where(User.is_banned.is_(False), User.is_test.is_(False))
            .order_by(User.rating.desc(), User.wins.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def top_by_wins(self, limit: int = 10) -> list[User]:
        result = await self.session.execute(
            select(User)
            .where(User.is_banned.is_(False), User.is_test.is_(False))
            .order_by(User.wins.desc(), User.rating.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def top_by_level(self, limit: int = 10) -> list[User]:
        result = await self.session.execute(
            select(User)
            .where(User.is_banned.is_(False), User.is_test.is_(False))
            .order_by(User.level.desc(), User.xp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def recent(self, limit: int = 10) -> list[User]:
        """Последние зарегистрированные игроки."""
        result = await self.session.execute(
            select(User)
            .where(User.is_banned.is_(False), User.is_test.is_(False))
            .order_by(User.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_all(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(User).where(User.is_banned.is_(False), User.is_test.is_(False))
        )
        return int(result.scalar_one())

    async def ids_for_broadcast(self) -> list[int]:
        """telegram_id всех, кто может получать ЛС."""
        result = await self.session.execute(
            select(User.telegram_id).where(
                User.is_banned.is_(False),
                User.can_receive_dm.is_(True),
                User.is_test.is_(False),  # тестовых ботов в рассылку не берём
            )
        )
        return [int(row[0]) for row in result.all()]

    async def set_banned(self, telegram_id: int, banned: bool) -> User | None:
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            user.is_banned = banned
            await self.session.flush()
        return user

    async def mark_cannot_receive_dm(self, user_id: int) -> None:
        await self.session.execute(
            update(User).where(User.id == user_id).values(can_receive_dm=False)
        )

    # ----------------------------------------------------- место в рейтингах

    async def rank_by_rating(self, rating: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(User).where(
                User.is_banned.is_(False), User.is_test.is_(False),
                User.rating > rating,
            )
        )
        return int(result.scalar_one()) + 1

    async def rank_by_wins(self, wins: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(User).where(
                User.is_banned.is_(False), User.is_test.is_(False),
                User.wins > wins,
            )
        )
        return int(result.scalar_one()) + 1

    async def rank_by_level(self, level: int, xp: int) -> int:
        # выше те, у кого уровень больше, либо уровень равен, но XP больше
        result = await self.session.execute(
            select(func.count()).select_from(User).where(
                User.is_banned.is_(False), User.is_test.is_(False),
                ((User.level > level) | ((User.level == level) & (User.xp > xp))),
            )
        )
        return int(result.scalar_one()) + 1
