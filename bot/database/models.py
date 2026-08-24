"""SQLAlchemy 2.x модели (async).

Соглашения:
- время хранится как наивный UTC (TIMESTAMP WITHOUT TIME ZONE);
- enum-подобные значения — строковые константы (переносимо между SQLite/PG);
- JSON-колонки хранят Pydantic-модели настроек (RoomSettings, GlobalSettings).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from bot.utils.helpers import utcnow


class Base(DeclarativeBase):
    pass


# --- Перечисления -------------------------------------------------------------


class RoomStatus(str, enum.Enum):
    OPEN = "OPEN"            # набор игроков
    PLAYING = "PLAYING"      # идёт игра
    CLOSED = "CLOSED"        # закрыта создателем
    FINISHED = "FINISHED"    # игра завершена


class GameStatus(str, enum.Enum):
    WAITING = "WAITING"      # (используется комнатой до старта)
    STARTING = "STARTING"    # обратный отсчёт перед первой ночью
    NIGHT = "NIGHT"
    DAY = "DAY"
    VOTING = "VOTING"
    ENDED = "ENDED"


class PlayerStatus(str, enum.Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"        # убит ночью или изгнан голосованием
    LEFT = "LEFT"        # вышел из игры добровольно
    SPECTATOR = "SPECTATOR"  # наблюдатель (не участвует)


class WinningSide(str, enum.Enum):
    MAFIA = "mafia"
    CITY = "city"
    MANIAC = "maniac"
    DRAW = "draw"        # ничья/принудительное завершение


# --- Модели -------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    show_username: Mapped[bool] = mapped_column(Boolean, default=False)

    games_played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    correct_votes: Mapped[int] = mapped_column(Integer, default=0)

    rating: Mapped[int] = mapped_column(Integer, default=1000)
    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    can_receive_dm: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} tg={self.telegram_id}>"


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    max_players: Mapped[int] = mapped_column(Integer, default=10)
    min_players: Mapped[int] = mapped_column(Integer, default=4)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=RoomStatus.OPEN.value, index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)  # RoomSettings.model_dump()
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    players: Mapped[list["RoomPlayer"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", lazy="selectin",
        order_by="RoomPlayer.joined_at",
    )

    def player_count(self) -> int:
        return len(self.players)


class RoomPlayer(Base):
    __tablename__ = "room_players"
    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_room_player"),
        Index("ix_room_players_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    room: Mapped[Room] = relationship(back_populates="players")
    user: Mapped[User] = relationship(lazy="joined")


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default=GameStatus.STARTING.value, index=True)
    max_players: Mapped[int] = mapped_column(Integer, default=10)
    day_number: Mapped[int] = mapped_column(Integer, default=0)
    phase_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    winner: Mapped[str | None] = mapped_column(String(16), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Контекст текущего голосования: {"round_no": 1, "candidates": [user_id, ...]}
    vote_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Журнал событий для статистики и разбора: [{type, ...}, ...]
    events: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    players: Mapped[list["GamePlayer"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", lazy="selectin",
        order_by="GamePlayer.slot",
    )

    @property
    def current_phase(self) -> str:
        """Алиас: фаза == статусу для NIGHT/DAY/VOTING."""
        return self.status

    def get_setting(self, key: str, default=None):
        return (self.settings or {}).get(key, default)


class GamePlayer(Base):
    __tablename__ = "game_players"
    __table_args__ = (
        UniqueConstraint("game_id", "user_id", name="uq_game_player"),
        Index("ix_game_players_game_alive", "game_id", "is_alive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=PlayerStatus.ALIVE.value)
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True)
    slot: Mapped[int] = mapped_column(Integer, default=0)  # номер места
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    died_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    death_cause: Mapped[str | None] = mapped_column(String(32), nullable=True)  # mafia|maniac|vote|left|sacrifice

    game: Mapped[Game] = relationship(back_populates="players")
    user: Mapped[User] = relationship(lazy="joined")


class GameAction(Base):
    __tablename__ = "game_actions"
    __table_args__ = (
        UniqueConstraint("game_id", "day_number", "actor_id", "action_type", name="uq_night_action"),
        Index("ix_actions_game_day", "game_id", "day_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(16))  # kill|heal|check|block|protect
    phase: Mapped[str] = mapped_column(String(16), default="NIGHT")
    day_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("game_id", "day_number", "round_no", "voter_id", name="uq_vote"),
        Index("ix_votes_game_day_round", "game_id", "day_number", "round_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    voter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    day_number: Mapped[int] = mapped_column(Integer, default=1)
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Ключ-значение для глобальных настроек (роль-менеджмент и т.п.)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
