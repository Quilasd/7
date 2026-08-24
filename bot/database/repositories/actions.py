"""Репозиторий ночных действий."""

from __future__ import annotations

from sqlalchemy import select

from bot.database.models import GameAction
from bot.database.repositories.base import BaseRepository


class GameActionRepository(BaseRepository[GameAction]):
    model = GameAction

    async def upsert(
        self,
        game_id: int,
        actor_id: int,
        target_id: int,
        action_type: str,
        day_number: int,
        phase: str = "NIGHT",
    ) -> GameAction:
        """Идемпотентная запись: повторный выбор цели обновляет существующую.

        Так обрабатывается «двойное нажатие» и осознанная смена цели ночью.
        """
        action = await self.get_action(game_id, actor_id, action_type, day_number)
        if action is None:
            action = GameAction(
                game_id=game_id,
                actor_id=actor_id,
                target_id=target_id,
                action_type=action_type,
                phase=phase,
                day_number=day_number,
            )
            self.session.add(action)
        else:
            action.target_id = target_id
        await self.session.flush()
        return action

    async def get_action(
        self, game_id: int, actor_id: int, action_type: str, day_number: int
    ) -> GameAction | None:
        result = await self.session.execute(
            select(GameAction).where(
                GameAction.game_id == game_id,
                GameAction.actor_id == actor_id,
                GameAction.action_type == action_type,
                GameAction.day_number == day_number,
            )
        )
        return result.scalar_one_or_none()

    async def night_actions(self, game_id: int, day_number: int) -> list[GameAction]:
        result = await self.session.execute(
            select(GameAction).where(
                GameAction.game_id == game_id,
                GameAction.day_number == day_number,
                GameAction.phase == "NIGHT",
            )
        )
        return list(result.scalars().all())

    async def actions_of_type(
        self, game_id: int, action_type: str
    ) -> list[GameAction]:
        """Все действия одного типа за всю игру (для статистики)."""
        result = await self.session.execute(
            select(GameAction).where(
                GameAction.game_id == game_id,
                GameAction.action_type == action_type,
            ).order_by(GameAction.day_number)
        )
        return list(result.scalars().all())
