"""Сервис комнат (лобби): создание, вход/выход, готовность, кик, настройки.

RoomSettings сериализуется в JSON-колонку rooms.settings — при старте игры
копируется в games.settings, поэтому изменение настроек после старта
невозможно по построению.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Room, RoomPlayer, RoomStatus
from bot.database.repositories.rooms import RoomPlayerRepository, RoomRepository
from bot.services.app_config import AppConfigService
from bot.services.notifier import Notifier
from bot.services.role_manager import validate_setup
from bot.utils.helpers import display_name, esc, hash_password, utcnow, verify_password

logger = logging.getLogger(__name__)


class RoomSettings(BaseModel):
    """Настройки комнаты (сохраняются в rooms.settings как JSON)."""

    roles: dict[str, int] = Field(default_factory=lambda: {"mafia": 1, "detective": 1, "doctor": 1})
    night_seconds: int = 90
    day_seconds: int = 180
    vote_seconds: int = 60
    start_countdown_seconds: int = 5
    tie_rule: Literal["revote", "no_death"] = "revote"
    reveal_roles_on_death: bool = True

    @staticmethod
    def from_room(room: Room) -> "RoomSettings":
        data = dict(room.settings or {})
        defaults = RoomSettings().model_dump()
        return RoomSettings(**{**defaults, **data})


class RoomService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        notifier: Notifier,
        app_config: AppConfigService,
        max_players_limit: int = 20,
        min_players_limit: int = 4,
    ) -> None:
        self.session_factory = session_factory
        self.notifier = notifier
        self.app_config = app_config
        self.max_players_limit = max_players_limit
        self.min_players_limit = min_players_limit

    async def default_settings(self) -> RoomSettings:
        gs = await self.app_config.get()
        return RoomSettings(
            night_seconds=gs.night_seconds,
            day_seconds=gs.day_seconds,
            vote_seconds=gs.vote_seconds,
        )

    async def create_room(
        self,
        creator_user_id: int,
        name: str,
        max_players: int,
        min_players: int,
        is_private: bool,
        password: str | None,
        roles: dict[str, int],
        group_id: int | None = None,
    ) -> tuple[Room | None, str]:
        """Создание комнаты визардом. group_id=None — глобальная комната (ЛС),
        group_id=<группы> — комната ЭТОЙ группы (визард, запущенный в группе)."""
        name = name.strip()[:64]
        if len(name) < 3:
            return None, "Название комнаты должно быть от 3 символов."
        if not (self.min_players_limit <= min_players <= max_players <= self.max_players_limit):
            return None, (
                f"Игроков: минимум {self.min_players_limit}, максимум {self.max_players_limit}."
            )
        if is_private and (password is None or not (4 <= len(password) <= 32)):
            return None, "Пароль приватной комнаты: 4–32 символа."
        errors = validate_setup(roles, max_players, min_players)
        if errors:
            return None, "; ".join(errors)

        defaults = await self.default_settings()
        defaults.roles = {k: v for k, v in roles.items() if k != "citizen"}

        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            existing = await rooms.open_room_of_user(creator_user_id)
            if existing:
                return None, f"Ты уже в комнате #{existing.id}. Сначала покинь её."
            room = Room(
                creator_id=creator_user_id,
                name=name,
                max_players=max_players,
                min_players=min_players,
                is_private=is_private,
                password_hash=hash_password(password) if is_private else None,
                status=RoomStatus.OPEN.value,
                settings=defaults.model_dump(mode="json"),
                group_id=group_id,
            )
            session.add(room)
            await session.flush()
            session.add(RoomPlayer(room_id=room.id, user_id=creator_user_id, is_ready=False))
            await session.commit()
            logger.info("Создана комната %s (%s игроков, приватная=%s)", room.id, max_players, is_private)
            return room, "Комната создана!"

    async def join(self, room_id: int, user_id: int, password: str | None = None) -> tuple[Room | None, str]:
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None or room.status != RoomStatus.OPEN.value:
                return None, "Комната не найдена или недоступна."
            if room.creator_id != user_id and room.is_private:
                if not password or not verify_password(password, room.password_hash):
                    return None, "🔐 Неверный пароль комнаты."

            players = RoomPlayerRepository(session)
            membership = await players.get_membership(room.id, user_id)
            if membership:
                return room, "Ты уже в этой комнате."

            other_open = await rooms.open_room_of_user(user_id)
            if other_open and other_open.id != room.id:
                return None, f"Ты уже в комнате #{other_open.id}. Сначала покинь её."

            if room.player_count() >= room.max_players:
                return None, "Комната заполнена."

            # локальный бан в группе этой комнаты (лениво снимаем истёкший)
            if room.group_id:
                from bot.services.groups import effective_ban

                banned, gp = await effective_ban(session, room.group_id, user_id)
                if banned:
                    until = f" до {gp.banned_until:%d.%m.%Y %H:%M}" if gp and gp.banned_until else ""
                    return None, f"🚫 Ты забанен в этой группе{until}."

            session.add(RoomPlayer(room_id=room.id, user_id=user_id, is_ready=False))
            await session.commit()
            logger.info("Комната %s: присоединился игрок %s", room.id, user_id)
            return room, "Ты в комнате!"

    async def leave(self, room_id: int, user_id: int) -> tuple[Room | None, str]:
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None or room.status != RoomStatus.OPEN.value:
                return None, "Комната недоступна."
            players = RoomPlayerRepository(session)
            membership = await players.get_membership(room.id, user_id)
            if membership is None:
                return None, "Ты не в этой комнате."
            if room.creator_id == user_id:
                # Создатель не «выходит», а закрывает комнату целиком
                return None, "Создатель не может покинуть комнату — можно только закрыть её (❌)."

            await session.delete(membership)
            await session.commit()
            return room, "Ты покинул комнату."

    async def set_ready(self, room_id: int, user_id: int, ready: bool) -> tuple[Room | None, str]:
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None or room.status != RoomStatus.OPEN.value:
                return None, "Комната недоступна."
            players = RoomPlayerRepository(session)
            membership = await players.get_membership(room.id, user_id)
            if membership is None:
                return None, "Ты не в этой комнате."
            membership.is_ready = ready
            await session.commit()
            return room, "🟢 Готов!" if ready else "🟡 Не готов."

    async def kick(self, room_id: int, creator_user_id: int, target_user_id: int) -> tuple[Room | None, str]:
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None or room.status != RoomStatus.OPEN.value:
                return None, "Комната недоступна."
            if room.creator_id != creator_user_id:
                return None, "Исключать игроков может только создатель."
            if target_user_id == creator_user_id:
                return None, "Нельзя исключить себя."
            players = RoomPlayerRepository(session)
            membership = await players.get_membership(room.id, target_user_id)
            if membership is None:
                return None, "Игрок не в комнате."
            target_name = esc(display_name(membership.user))
            await session.delete(membership)
            await session.commit()
            await self.notifier.send(
                membership.user.telegram_id,
                f"🚫 Тебя исключили из комнаты #{room.id} «{esc(room.name)}».",
            )
            logger.info("Комната %s: создатель %s исключил %s", room.id, creator_user_id, target_user_id)
            return room, f"Игрок {target_name} исключён."

    async def close_room(self, room_id: int, creator_user_id: int) -> str:
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None:
                return "Комната не найдена."
            if room.creator_id != creator_user_id:
                return "Закрыть комнату может только создатель."
            if room.status == RoomStatus.PLAYING.value:
                return "Игра уже идёт — дождись её окончания или обратись к админу."
            members = room.players
            room.status = RoomStatus.CLOSED.value
            room.closed_at = utcnow()
            await session.commit()
            for membership in members:
                if membership.user_id != creator_user_id:
                    await self.notifier.send(
                        membership.user.telegram_id,
                        f"🚪 Комната #{room.id} «{esc(room.name)}» закрыта создателем.",
                    )
            logger.info("Комната %s закрыта", room.id)
            return "Комната закрыта."

    async def update_settings(
        self, room_id: int, creator_user_id: int, mutate
    ) -> tuple[Room | None, str]:
        """Применяет функцию mutate(RoomSettings) -> RoomSettings."""
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None:
                return None, "Комната не найдена."
            if room.creator_id != creator_user_id:
                return None, "Настройки меняет только создатель."
            if room.status != RoomStatus.OPEN.value:
                return None, "После старта игры настройки менять нельзя."
            settings = RoomSettings.from_room(room)
            settings = mutate(settings)
            errors = validate_setup(settings.roles, room.max_players, room.min_players)
            if errors:
                return room, "❌ " + "; ".join(errors)
            room.settings = settings.model_dump(mode="json")
            await session.commit()
            return room, "✅ Настройки сохранены."
