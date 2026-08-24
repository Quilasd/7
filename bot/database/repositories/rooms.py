"""Репозитории комнат и участников комнат."""

from __future__ import annotations

from sqlalchemy import select

from bot.database.models import Room, RoomPlayer, RoomStatus
from bot.database.repositories.base import BaseRepository


class RoomRepository(BaseRepository[Room]):
    model = Room

    async def get(self, room_id: int) -> Room | None:
        result = await self.session.execute(
            select(Room).where(Room.id == room_id)
        )
        return result.scalar_one_or_none()

    async def open_public_rooms(self, limit: int = 10) -> list[Room]:
        result = await self.session.execute(
            select(Room)
            .where(Room.status == RoomStatus.OPEN.value, Room.is_private.is_(False))
            .order_by(Room.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def open_room_of_user(self, user_id: int) -> Room | None:
        """Комната со статусом OPEN, в которой состоит игрок."""
        result = await self.session.execute(
            select(Room)
            .join(RoomPlayer, RoomPlayer.room_id == Room.id)
            .where(RoomPlayer.user_id == user_id, Room.status == RoomStatus.OPEN.value)
        )
        return result.scalars().unique().one_or_none()

    async def count_open(self) -> int:
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(Room)
            .where(Room.status == RoomStatus.OPEN.value)
        )
        return int(result.scalar_one())

    async def list_open(self, limit: int = 20) -> list[Room]:
        result = await self.session.execute(
            select(Room)
            .where(Room.status.in_([RoomStatus.OPEN.value, RoomStatus.PLAYING.value]))
            .order_by(Room.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())


class RoomPlayerRepository(BaseRepository[RoomPlayer]):
    model = RoomPlayer

    async def get(self, room_player_id: int) -> RoomPlayer | None:
        return await self.session.get(RoomPlayer, room_player_id)

    async def get_membership(self, room_id: int, user_id: int) -> RoomPlayer | None:
        result = await self.session.execute(
            select(RoomPlayer).where(
                RoomPlayer.room_id == room_id, RoomPlayer.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_room(self, room_id: int) -> list[RoomPlayer]:
        result = await self.session.execute(
            select(RoomPlayer)
            .where(RoomPlayer.room_id == room_id)
            .order_by(RoomPlayer.joined_at)
        )
        return list(result.scalars().unique().all())

    async def user_ids_in_room(self, room_id: int) -> set[int]:
        rows = await self.session.execute(
            select(RoomPlayer.user_id).where(RoomPlayer.room_id == room_id)
        )
        return {int(r[0]) for r in rows.all()}
