"""Репозитории игр и игроков игры."""

from __future__ import annotations

from sqlalchemy import func, select

from bot.database.models import Game, GamePlayer, GameStatus, PlayerStatus
from bot.database.repositories.base import BaseRepository

ACTIVE_STATUSES = [
    GameStatus.STARTING.value,
    GameStatus.NIGHT.value,
    GameStatus.DAY.value,
    GameStatus.VOTING.value,
]


class GameRepository(BaseRepository[Game]):
    model = Game

    async def get(self, game_id: int) -> Game | None:
        result = await self.session.execute(select(Game).where(Game.id == game_id))
        return result.scalars().unique().one_or_none()

    async def active_games(self) -> list[Game]:
        result = await self.session.execute(
            select(Game).where(Game.status.in_(ACTIVE_STATUSES)).order_by(Game.created_at)
        )
        return list(result.scalars().unique().all())

    async def count_active(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Game).where(Game.status.in_(ACTIVE_STATUSES))
        )
        return int(result.scalar_one())

    async def count_finished(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Game).where(Game.status == GameStatus.ENDED.value)
        )
        return int(result.scalar_one())

    async def count_active_today(self) -> int:
        from bot.utils.helpers import utcnow
        from datetime import timedelta

        since = utcnow() - timedelta(hours=24)
        result = await self.session.execute(
            select(func.count())
            .select_from(Game)
            .where(Game.created_at >= since, Game.status != GameStatus.WAITING.value)
        )
        return int(result.scalar_one())


class GamePlayerRepository(BaseRepository[GamePlayer]):
    model = GamePlayer

    async def get(self, game_player_id: int) -> GamePlayer | None:
        return await self.session.get(GamePlayer, game_player_id)

    async def get_by_user(self, game_id: int, user_id: int) -> GamePlayer | None:
        result = await self.session.execute(
            select(GamePlayer).where(
                GamePlayer.game_id == game_id, GamePlayer.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_game(self, game_id: int) -> list[GamePlayer]:
        result = await self.session.execute(
            select(GamePlayer).where(GamePlayer.game_id == game_id).order_by(GamePlayer.slot)
        )
        return list(result.scalars().unique().all())

    async def alive(self, game_id: int) -> list[GamePlayer]:
        result = await self.session.execute(
            select(GamePlayer)
            .where(GamePlayer.game_id == game_id, GamePlayer.is_alive.is_(True))
            .order_by(GamePlayer.slot)
        )
        return list(result.scalars().unique().all())

    async def alive_count(self, game_id: int) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(GamePlayer)
            .where(GamePlayer.game_id == game_id, GamePlayer.is_alive.is_(True))
        )
        return int(result.scalar_one())

    async def active_game_of_user(self, user_id: int) -> GamePlayer | None:
        """Игра, в которой пользователь сейчас участвует (не завершена)."""
        result = await self.session.execute(
            select(GamePlayer)
            .join(Game, Game.id == GamePlayer.game_id)
            .where(GamePlayer.user_id == user_id, Game.status.in_(ACTIVE_STATUSES))
        )
        return result.scalars().unique().one_or_none()

    async def history_for_user(self, user_id: int, limit: int = 10) -> list[GamePlayer]:
        result = await self.session.execute(
            select(GamePlayer)
            .join(Game, Game.id == GamePlayer.game_id)
            .where(GamePlayer.user_id == user_id, Game.status == GameStatus.ENDED.value)
            .order_by(Game.ended_at.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def by_role(self, game_id: int, role_id: str) -> list[GamePlayer]:
        result = await self.session.execute(
            select(GamePlayer).where(
                GamePlayer.game_id == game_id,
                GamePlayer.role == role_id,
                GamePlayer.status != PlayerStatus.SPECTATOR.value,
            )
        )
        return list(result.scalars().unique().all())
