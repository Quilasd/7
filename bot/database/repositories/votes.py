"""Репозиторий голосов дневного голосования."""

from __future__ import annotations

from collections import Counter
from sqlalchemy import select

from bot.database.models import Vote
from bot.database.repositories.base import BaseRepository


class VoteRepository(BaseRepository[Vote]):
    model = Vote

    async def set_vote(
        self, game_id: int, voter_id: int, target_id: int, day_number: int, round_no: int
    ) -> Vote:
        """Идемпотентно: переголосование обновляет существующий голос."""
        vote = await self.get_vote(game_id, voter_id, day_number, round_no)
        if vote is None:
            vote = Vote(
                game_id=game_id,
                voter_id=voter_id,
                target_id=target_id,
                day_number=day_number,
                round_no=round_no,
            )
            self.session.add(vote)
        else:
            vote.target_id = target_id
        await self.session.flush()
        return vote

    async def get_vote(
        self, game_id: int, voter_id: int, day_number: int, round_no: int
    ) -> Vote | None:
        result = await self.session.execute(
            select(Vote).where(
                Vote.game_id == game_id,
                Vote.voter_id == voter_id,
                Vote.day_number == day_number,
                Vote.round_no == round_no,
            )
        )
        return result.scalar_one_or_none()

    async def round_votes(
        self, game_id: int, day_number: int, round_no: int
    ) -> list[Vote]:
        result = await self.session.execute(
            select(Vote).where(
                Vote.game_id == game_id,
                Vote.day_number == day_number,
                Vote.round_no == round_no,
            )
        )
        return list(result.scalars().all())

    async def tally(
        self, game_id: int, day_number: int, round_no: int
    ) -> list[tuple[int, int]]:
        """[(target_user_id, голосов)], отсортировано по убыванию."""
        votes = await self.round_votes(game_id, day_number, round_no)
        counter = Counter(v.target_id for v in votes)
        return sorted(counter.items(), key=lambda item: (-item[1], item[0]))

    async def game_votes(self, game_id: int) -> list[Vote]:
        result = await self.session.execute(
            select(Vote).where(Vote.game_id == game_id)
        )
        return list(result.scalars().all())
