"""Репозитории социальных функций: предсмертные записки, друзья, игнор,
избранное, достижения, титулы, ивентовые награды.

Работают поверх существующих моделей и сессий; не дублируют логику банов/мутов
и permissions — только хранение/выборка.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select

from bot.database.models import (
    DeathNote,
    EventReward,
    FavoritePlayer,
    Friendship,
    FriendRequest,
    UserAchievement,
    UserBlock,
    UserEventReward,
    UserTitle,
)
from bot.database.repositories.base import BaseRepository
from bot.utils.helpers import utcnow


# ------------------------------------------------------------------ записки


class DeathNoteRepository(BaseRepository[DeathNote]):
    model = DeathNote

    async def get(self, game_id: int, user_id: int) -> DeathNote | None:
        result = await self.session.execute(
            select(DeathNote).where(
                DeathNote.game_id == game_id, DeathNote.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def ensure(self, game_id: int, user_id: int, death_day: int) -> DeathNote:
        """Создаёт placeholder при первой смерти; не затирает существующий текст."""
        note = await self.get(game_id, user_id)
        if note is None:
            note = DeathNote(game_id=game_id, user_id=user_id, death_day=death_day)
            self.session.add(note)
            await self.session.flush()
        return note

    async def set_text(self, game_id: int, user_id: int, text: str) -> DeathNote | None:
        note = await self.get(game_id, user_id)
        if note is None or note.text is not None:
            return None  # либо ещё не умер, либо уже написал (неизменяемо)
        note.text = text[:300]
        await self.session.flush()
        return note

    async def pending_before(self, game_id: int, day: int) -> list[DeathNote]:
        """Неопубликованные записки игроков, умерших строго ранее дня `day`."""
        result = await self.session.execute(
            select(DeathNote).where(
                DeathNote.game_id == game_id,
                DeathNote.published.is_(False),
                DeathNote.death_day < day,
            ).order_by(DeathNote.death_day, DeathNote.id)
        )
        return list(result.scalars().all())

    async def unpublished(self, game_id: int) -> list[DeathNote]:
        result = await self.session.execute(
            select(DeathNote).where(
                DeathNote.game_id == game_id, DeathNote.published.is_(False)
            )
        )
        return list(result.scalars().all())


# ------------------------------------------------------------------- друзья


class FriendRequestRepository(BaseRepository[FriendRequest]):
    model = FriendRequest

    async def get(self, from_id: int, to_id: int) -> FriendRequest | None:
        result = await self.session.execute(
            select(FriendRequest).where(
                FriendRequest.from_user_id == from_id, FriendRequest.to_user_id == to_id
            )
        )
        return result.scalar_one_or_none()

    async def pending_to(self, user_id: int) -> list[FriendRequest]:
        result = await self.session.execute(
            select(FriendRequest).where(FriendRequest.to_user_id == user_id)
            .order_by(FriendRequest.created_at.desc())
        )
        return list(result.scalars().all())

    async def add(self, from_id: int, to_id: int) -> FriendRequest:
        req = FriendRequest(from_user_id=from_id, to_user_id=to_id)
        self.session.add(req)
        await self.session.flush()
        return req

    async def remove(self, from_id: int, to_id: int) -> bool:
        result = await self.session.execute(
            delete(FriendRequest).where(
                FriendRequest.from_user_id == from_id, FriendRequest.to_user_id == to_id
            )
        )
        return result.rowcount > 0


class FriendshipRepository(BaseRepository[Friendship]):
    model = Friendship

    async def are_friends(self, user_id: int, other_id: int) -> bool:
        result = await self.session.execute(
            select(Friendship.id).where(
                Friendship.user_id == user_id, Friendship.friend_id == other_id
            )
        )
        return result.scalar_one_or_none() is not None

    async def add(self, user_id: int, friend_id: int) -> None:
        self.session.add(Friendship(user_id=user_id, friend_id=friend_id))
        await self.session.flush()

    async def remove(self, user_id: int, friend_id: int) -> bool:
        """Дружба двунаправленная — снимаем обе строки."""
        result = await self.session.execute(
            delete(Friendship).where(
                or_pair(Friendship.user_id, Friendship.friend_id, user_id, friend_id)
            )
        )
        await self.session.flush()
        return result.rowcount > 0

    async def list_friends(self, user_id: int) -> list[int]:
        result = await self.session.execute(
            select(Friendship.friend_id).where(Friendship.user_id == user_id)
        )
        return [int(r[0]) for r in result.all()]

    async def count_friends(self, user_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Friendship)
            .where(Friendship.user_id == user_id)
        )
        return int(result.scalar_one())


def or_pair(user_col, friend_col, user_id: int, friend_id: int):
    """(A дружит с B) OR (B дружит с A)."""
    return (
        ((user_col == user_id) & (friend_col == friend_id))
        | ((user_col == friend_id) & (friend_col == user_id))
    )


# ----------------------------------------------------------- игнор / избранное


class UserBlockRepository(BaseRepository[UserBlock]):
    model = UserBlock

    async def is_blocked(self, user_id: int, blocked_id: int) -> bool:
        result = await self.session.execute(
            select(UserBlock.id).where(
                UserBlock.user_id == user_id, UserBlock.blocked_id == blocked_id
            )
        )
        return result.scalar_one_or_none() is not None

    async def add(self, user_id: int, blocked_id: int) -> bool:
        if await self.is_blocked(user_id, blocked_id):
            return False
        self.session.add(UserBlock(user_id=user_id, blocked_id=blocked_id))
        await self.session.flush()
        return True

    async def remove(self, user_id: int, blocked_id: int) -> bool:
        result = await self.session.execute(
            delete(UserBlock).where(
                UserBlock.user_id == user_id, UserBlock.blocked_id == blocked_id
            )
        )
        return result.rowcount > 0

    async def blocked_ids(self, user_id: int) -> list[int]:
        result = await self.session.execute(
            select(UserBlock.blocked_id).where(UserBlock.user_id == user_id)
        )
        return [int(r[0]) for r in result.all()]


class FavoriteRepository(BaseRepository[FavoritePlayer]):
    model = FavoritePlayer

    async def is_favorite(self, user_id: int, favorite_id: int) -> bool:
        result = await self.session.execute(
            select(FavoritePlayer.id).where(
                FavoritePlayer.user_id == user_id, FavoritePlayer.favorite_id == favorite_id
            )
        )
        return result.scalar_one_or_none() is not None

    async def add(self, user_id: int, favorite_id: int) -> bool:
        if await self.is_favorite(user_id, favorite_id):
            return False
        self.session.add(FavoritePlayer(user_id=user_id, favorite_id=favorite_id))
        await self.session.flush()
        return True

    async def remove(self, user_id: int, favorite_id: int) -> bool:
        result = await self.session.execute(
            delete(FavoritePlayer).where(
                FavoritePlayer.user_id == user_id, FavoritePlayer.favorite_id == favorite_id
            )
        )
        return result.rowcount > 0

    async def list_ids(self, user_id: int) -> list[int]:
        result = await self.session.execute(
            select(FavoritePlayer.favorite_id).where(FavoritePlayer.user_id == user_id)
        )
        return [int(r[0]) for r in result.all()]


# ---------------------------------------------------------- достижения/титулы


class UserAchievementRepository(BaseRepository[UserAchievement]):
    model = UserAchievement

    async def has(self, user_id: int, achievement_id: str) -> bool:
        result = await self.session.execute(
            select(UserAchievement.id).where(
                UserAchievement.user_id == user_id,
                UserAchievement.achievement_id == achievement_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def award(self, user_id: int, achievement_id: str) -> bool:
        """True — выдано сейчас (не было раньше). Одноразовое."""
        if await self.has(user_id, achievement_id):
            return False
        self.session.add(UserAchievement(user_id=user_id, achievement_id=achievement_id))
        await self.session.flush()
        return True

    async def ids_of(self, user_id: int) -> set[str]:
        result = await self.session.execute(
            select(UserAchievement.achievement_id).where(UserAchievement.user_id == user_id)
        )
        return {str(r[0]) for r in result.all()}

    async def count(self, user_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(UserAchievement)
            .where(UserAchievement.user_id == user_id)
        )
        return int(result.scalar_one())

    async def remove(self, user_id: int, achievement_id: str) -> bool:
        """Снимает достижение вручную (OWNER). True — если оно было."""
        result = await self.session.execute(
            delete(UserAchievement).where(
                UserAchievement.user_id == user_id,
                UserAchievement.achievement_id == achievement_id,
            )
        )
        return bool(result.rowcount)


class UserTitleRepository(BaseRepository[UserTitle]):
    model = UserTitle

    async def remove(self, user_id: int, title_id: str, source: str | None = None) -> bool:
        """Снимает открытый титул (опционально только с данным source)."""
        stmt = delete(UserTitle).where(
            UserTitle.user_id == user_id, UserTitle.title_id == title_id
        )
        if source is not None:
            stmt = stmt.where(UserTitle.source == source)
        result = await self.session.execute(stmt)
        return bool(result.rowcount)

    async def unlock(self, user_id: int, title_id: str, source: str = "achievement") -> bool:
        result = await self.session.execute(
            select(UserTitle.id).where(
                UserTitle.user_id == user_id, UserTitle.title_id == title_id
            )
        )
        if result.scalar_one_or_none() is not None:
            return False
        self.session.add(UserTitle(user_id=user_id, title_id=title_id, source=source))
        await self.session.flush()
        return True

    async def ids_of(self, user_id: int) -> list[str]:
        result = await self.session.execute(
            select(UserTitle.title_id).where(UserTitle.user_id == user_id)
        )
        return [str(r[0]) for r in result.all()]


# ------------------------------------------------------------ ивентовые награды


class EventRewardRepository(BaseRepository[EventReward]):
    model = EventReward

    async def get_by_code(self, code: str) -> EventReward | None:
        result = await self.session.execute(
            select(EventReward).where(EventReward.code == code)
        )
        return result.scalar_one_or_none()

    async def all(self) -> list[EventReward]:
        result = await self.session.execute(
            select(EventReward).order_by(EventReward.id)
        )
        return list(result.scalars().all())

    async def create(self, code: str, name: str, emoji: str, description: str,
                     kind: str, expires_days: int | None, created_by: int) -> EventReward:
        reward = EventReward(
            code=code, name=name[:64], emoji=emoji[:16], description=description[:256],
            kind=kind, expires_days=expires_days, created_by=created_by,
        )
        self.session.add(reward)
        await self.session.flush()
        return reward


class UserEventRewardRepository(BaseRepository[UserEventReward]):
    model = UserEventReward

    async def grant(self, user_id: int, reward_id: int, awarded_by: int,
                    expires_at=None) -> UserEventReward:
        row = UserEventReward(
            user_id=user_id, reward_id=reward_id, awarded_by=awarded_by,
            awarded_at=utcnow(), expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def of_user(self, user_id: int) -> list[UserEventReward]:
        result = await self.session.execute(
            select(UserEventReward).where(UserEventReward.user_id == user_id)
            .order_by(UserEventReward.awarded_at.desc())
        )
        return list(result.scalars().all())

    async def active_for(self, user_id: int) -> UserEventReward | None:
        """Активная (не истекшая) награда. Истёкшие остаются в истории."""
        result = await self.session.execute(
            select(UserEventReward).where(
                UserEventReward.user_id == user_id,
                (UserEventReward.expires_at.is_(None)) | (UserEventReward.expires_at > utcnow()),
            ).order_by(UserEventReward.awarded_at.desc())
        )
        return result.scalars().first()


__all__ = [
    "DeathNoteRepository", "FriendRequestRepository", "FriendshipRepository",
    "UserBlockRepository", "FavoriteRepository", "UserAchievementRepository",
    "UserTitleRepository", "EventRewardRepository", "UserEventRewardRepository",
]
