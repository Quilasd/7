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
    BigInteger,
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
    investigations: Mapped[int] = mapped_column(Integer, default=0)
    correct_votes: Mapped[int] = mapped_column(Integer, default=0)

    rating: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    xp: Mapped[int] = mapped_column(Integer, default=0)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    can_receive_dm: Mapped[bool] = mapped_column(Boolean, default=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # 🔥 Серия побед (одна общая — любая победа продлевает, поражение сбрасывает)
    win_streak: Mapped[int] = mapped_column(Integer, default=0)
    best_win_streak: Mapped[int] = mapped_column(Integer, default=0)

    # Выбор пользователя (один активный титул / одна активная ивентовая награда)
    active_title: Mapped[str | None] = mapped_column(String(48), nullable=True)
    active_event_reward_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True, index=True)

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
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True, index=True)
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


# --- Группы, локальная статистика, администрация -------------------------------


class Group(Base):
    """Telegram-группа со своими настройками, админами и локальными рейтингами."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Group id={self.id} chat={self.telegram_chat_id} {self.title!r}>"


class GroupPlayer(Base):
    """Участник группы и его ЛОКАЛЬНАЯ статистика (полностью отделена от User)."""

    __tablename__ = "group_players"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_player"),
        Index("ix_group_players_top", "group_id", "rating"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    # Локальная статистика
    games_played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    kills: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    investigations: Mapped[int] = mapped_column(Integer, default=0)
    correct_votes: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[int] = mapped_column(Integer, default=0)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    # 🔥 Локальная серия побед (в рамках этой группы)
    win_streak: Mapped[int] = mapped_column(Integer, default=0)
    best_win_streak: Mapped[int] = mapped_column(Integer, default=0)

    # Модерация внутри группы
    warnings: Mapped[int] = mapped_column(Integer, default=0)  # активные варны (синхронизируется с group_warnings)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    banned_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None = навсегда

    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(lazy="joined")


class GroupAdmin(Base):
    """Администратор группы с уровнем 1..5 (см. AdminLevel)."""

    __tablename__ = "group_admins"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_admin"),
        Index("ix_group_admins_group", "group_id", "admin_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    admin_level: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(lazy="joined")


class GroupSettingsModel(Base):
    """Настройки правил игры конкретной группы (Mafia Online)."""

    __tablename__ = "group_settings"

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    min_players: Mapped[int] = mapped_column(Integer, default=4)
    max_players: Mapped[int] = mapped_column(Integer, default=10)
    night_seconds: Mapped[int] = mapped_column(Integer, default=90)
    day_seconds: Mapped[int] = mapped_column(Integer, default=180)
    discussion_seconds: Mapped[int] = mapped_column(Integer, default=180)
    vote_seconds: Mapped[int] = mapped_column(Integer, default=60)
    tie_rule: Mapped[str] = mapped_column(String(16), default="revote")
    role_reveal_on_death: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled_roles: Mapped[list] = mapped_column(JSON, default=list)  # пусто = все роли
    mafia_count: Mapped[int] = mapped_column(Integer, default=1)
    allow_maniac: Mapped[bool] = mapped_column(Boolean, default=True)
    xp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rating_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    global_xp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    local_xp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    global_rating_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    local_rating_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    debug_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Система варнов: лимит до авто-бана, срок жизни варна (часы), длительность авто-бана (мин)
    warn_limit: Mapped[int] = mapped_column(Integer, default=3)
    warn_expire_hours: Mapped[int] = mapped_column(Integer, default=168)      # 7 дней
    warn_ban_minutes: Mapped[int] = mapped_column(Integer, default=1440)     # 24 часа
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Журнал административных действий."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_group", "group_id", "created_at"),
        Index("ix_audit_actor", "actor_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(48))
    details: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Предсмертные записки, соцсеть, достижения, титулы, ивенты ---------------


class GroupWarning(Base):
    """Варн в группе: с причиной и сроком действия.

    Активные варны (expires_at > now, не отозван) считаются в GroupPlayer.warnings;
    при достижении лимита (GroupSettings.warn_limit) — авто-бан на время.
    """

    __tablename__ = "group_warnings"
    __table_args__ = (
        Index("ix_group_warnings_group_user", "group_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)  # срок действия варна
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)  # снят / израсходован авто-баном


class DeathNote(Base):
    """Предсмертная записка игрока в конкретной партии.

    Одна на (game_id, user_id). text=None — игрок ещё не написал; публикуется
    в утренней сводке (phase_manager). После записи текст неизменяем.
    """

    __tablename__ = "death_notes"
    __table_args__ = (
        UniqueConstraint("game_id", "user_id", name="uq_death_note"),
        Index("ix_death_notes_pending", "game_id", "published"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    death_day: Mapped[int] = mapped_column(Integer, default=0)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FriendRequest(Base):
    """Исходящий запрос в друзья (ожидающий принятия)."""

    __tablename__ = "friend_requests"
    __table_args__ = (
        UniqueConstraint("from_user_id", "to_user_id", name="uq_friend_request"),
        Index("ix_friend_requests_to", "to_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Friendship(Base):
    """Состоявшаяся дружба (двунаправленная логически, хранится одной строкой)."""

    __tablename__ = "friendships"
    __table_args__ = (
        UniqueConstraint("user_id", "friend_id", name="uq_friendship"),
        Index("ix_friendships_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserBlock(Base):
    """Игнор-лист: user_id игнорирует blocked_id."""

    __tablename__ = "user_blocks"
    __table_args__ = (
        UniqueConstraint("user_id", "blocked_id", name="uq_user_block"),
        Index("ix_user_blocks_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FavoritePlayer(Base):
    """Избранные игроки (отдельно от друзей)."""

    __tablename__ = "favorite_players"
    __table_args__ = (
        UniqueConstraint("user_id", "favorite_id", name="uq_favorite"),
        Index("ix_favorites_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    favorite_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserAchievement(Base):
    """Полученные достижения (одноразовые: unique user+achievement)."""

    __tablename__ = "user_achievements"
    __table_args__ = (
        UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),
        Index("ix_user_achievements_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    achievement_id: Mapped[str] = mapped_column(String(48))
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserTitle(Base):
    """Открытые титулы игрока (через достижения / админом / ивентом)."""

    __tablename__ = "user_titles"
    __table_args__ = (
        UniqueConstraint("user_id", "title_id", name="uq_user_title"),
        Index("ix_user_titles_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title_id: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(24), default="achievement")
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EventReward(Base):
    """Каталог ивентовых наград/ролей (управляется администрацией).

    kind: 'badge' (награда в профиль) или 'role' (временная ивентовая роль —
    отдельна от игровых ролей). expires_days — срок действия по умолчанию.
    """

    __tablename__ = "event_rewards"
    __table_args__ = (
        UniqueConstraint("code", name="uq_event_reward_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(64))
    emoji: Mapped[str] = mapped_column(String(16), default="🎪")
    description: Mapped[str] = mapped_column(String(256), default="")
    kind: Mapped[str] = mapped_column(String(16), default="badge")  # badge | role
    expires_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserEventReward(Base):
    """Выданные игроку ивентовые награды (несколько; одна активная в профиле)."""

    __tablename__ = "user_event_rewards"
    __table_args__ = (
        Index("ix_user_event_rewards_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    reward_id: Mapped[int] = mapped_column(ForeignKey("event_rewards.id", ondelete="CASCADE"), index=True)
    awarded_by: Mapped[int] = mapped_column(Integer, default=0)
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
