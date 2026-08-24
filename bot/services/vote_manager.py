"""Менеджер голосования: подача голосов и подведение итогов."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bot.database.models import Game, GamePlayer, Vote
from bot.database.repositories.games import GamePlayerRepository
from bot.database.repositories.votes import VoteRepository

logger = logging.getLogger(__name__)


@dataclass
class VoteResolution:
    votes: list[tuple[int, int]] = field(default_factory=list)  # [(target_id, count)]
    leader_id: int | None = None
    leader_votes: int = 0
    is_tie: bool = False
    tied_ids: list[int] = field(default_factory=list)
    total_votes: int = 0
    voters_by_target: dict[int, list[int]] = field(default_factory=dict)

    @property
    def lynched(self) -> int | None:
        return None if self.is_tie else self.leader_id


class VoteManager:
    def __init__(self, session) -> None:
        self.session = session
        self.votes = VoteRepository(session)
        self.players = GamePlayerRepository(session)

    async def cast_vote(
        self,
        game: Game,
        voter: GamePlayer,
        target: GamePlayer,
    ) -> None:
        """Валидация выполнена вызывающим кодом; здесь — идемпотентная запись."""
        round_no = self.current_round(game)
        await self.votes.set_vote(
            game_id=game.id,
            voter_id=voter.user_id,
            target_id=target.user_id,
            day_number=game.day_number,
            round_no=round_no,
        )

    @staticmethod
    def current_round(game: Game) -> int:
        if game.vote_context and "round_no" in game.vote_context:
            return int(game.vote_context["round_no"])
        return 1

    @staticmethod
    def candidates(game: Game) -> list[int] | None:
        """None = голосовать можно за любого живого."""
        if game.vote_context and game.vote_context.get("candidates") is not None:
            return list(game.vote_context["candidates"])
        return None

    async def get_vote_of(self, game: Game, voter_user_id: int) -> Vote | None:
        return await self.votes.get_vote(
            game.id, voter_user_id, game.day_number, self.current_round(game)
        )

    async def resolve(self, game: Game) -> VoteResolution:
        round_no = self.current_round(game)
        tally = await self.votes.tally(game.id, game.day_number, round_no)
        votes = await self.votes.round_votes(game.id, game.day_number, round_no)

        resolution = VoteResolution(
            votes=tally,
            total_votes=len(votes),
        )
        for vote in votes:
            resolution.voters_by_target.setdefault(vote.target_id, []).append(vote.voter_id)

        if not tally:
            resolution.is_tie = True  # никто не голосовал — никто не умирает
            return resolution

        resolution.leader_id, resolution.leader_votes = tally[0]
        top = tally[0][1]
        resolution.tied_ids = sorted(t for t, count in tally if count == top)
        resolution.is_tie = len(resolution.tied_ids) > 1
        return resolution
