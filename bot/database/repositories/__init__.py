"""Экспорт репозиториев."""

from bot.database.repositories.actions import GameActionRepository
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.rooms import RoomPlayerRepository, RoomRepository
from bot.database.repositories.settings import AppSettingRepository
from bot.database.repositories.users import UserRepository
from bot.database.repositories.votes import VoteRepository

__all__ = [
    "GameActionRepository",
    "GamePlayerRepository",
    "GameRepository",
    "RoomPlayerRepository",
    "RoomRepository",
    "AppSettingRepository",
    "UserRepository",
    "VoteRepository",
]
