"""Фикстуры тестов: in-memory SQLite, сервисы с FakeNotifier и Noop-таймерами."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.database import init_db
from bot.database.models import Room, RoomPlayer, User
from bot.services.app_config import AppConfigService
from bot.services.audit import AuditService
from bot.services.game_chat import GameChatService, NoopGameChatGateway
from bot.services.game_manager import GameManager
from bot.services.groups import GroupService
from bot.services.permissions import PermissionService
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rating import RatingService
from bot.services.rooms import RoomService
from bot.services.rewards import RewardService
from bot.services.setup import GroupSetupService
from bot.services.social import SocialService
from bot.services.test_game import TestGameManager
from bot.services.timer_manager import NoopTimerManager
from bot.services.notifier import FakeNotifier


class SettingsStub:
    default_night_seconds = 60
    default_day_seconds = 60
    default_vote_seconds = 30
    start_countdown_seconds = 0
    min_players_limit = 4
    max_players_limit = 20
    debug_mode = True
    debug_affects_global_stats = False
    debug_affects_local_stats = False
    _admins: list[int] = []
    _owners: list[int] = []

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self._admins

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self._owners

    def admin_id_list(self) -> list[int]:
        return list(self._admins)

    def owner_id_list(self) -> list[int]:
        return list(self._owners)


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.get_event_loop_policy()


@pytest_asyncio.fixture()
async def engine(tmp_path):
    # Файловая SQLite на тест: сессии получают РАЗНЫЕ соединения из пула.
    # StaticPool + :memory: даёт одно общее соединение, и транзакции
    # параллельных сессий (супервизор тест-игры + игровой цикл) ломают
    # друг друга: чужой rollback откатывает не закоммиченные записи.
    db_file = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"timeout": 30},
    )
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture()
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture()
async def notifier():
    return FakeNotifier()


@pytest_asyncio.fixture()
async def services(session_factory, notifier):
    """Полный контейнер сервисов с фейковым нотификатором и без таймеров."""
    timers = NoopTimerManager()
    locks = GameLocks()
    app_settings = SettingsStub()
    rating = RatingService()
    game_chats = GameChatService(session_factory, NoopGameChatGateway(), notifier)
    phases = PhaseManager(
        session_factory, notifier, timers, locks, rating=rating, app_settings=app_settings,
        game_chats=game_chats,
    )
    games = GameManager(session_factory, notifier, phases, locks, game_chats=game_chats)
    app_config = AppConfigService(session_factory, SettingsStub())
    rooms = RoomService(session_factory, notifier, app_config)
    permissions = PermissionService(app_settings)
    groups = GroupService(session_factory, permissions)
    audit = AuditService(session_factory)
    test_games = TestGameManager(session_factory, games, phases, notifier, groups=groups)
    container = type("Services", (), {})()
    container.session_factory = session_factory
    container.notifier = notifier
    container.timers = timers
    container.phases = phases
    container.games = games
    container.app_config = app_config
    container.rooms = rooms
    container.test_games = test_games
    container.settings = app_settings
    container.permissions = permissions
    container.groups = groups
    container.social = SocialService(session_factory)
    container.rewards = RewardService(session_factory)
    container.setup = GroupSetupService(session_factory)
    container.audit = audit
    container.rating = rating
    container.maintenance = None
    container.game_chats = GameChatService(session_factory, NoopGameChatGateway(), notifier)
    yield container
    test_games.stop_all()
    timers.cancel_all()


# ------------------------------------------------------------------ helpers

_next_telegram_id = 1000


async def make_user(session, name: str | None = None) -> User:
    global _next_telegram_id
    _next_telegram_id += 1
    user = User(
        telegram_id=_next_telegram_id,
        username=name.lower() if name else None,
        display_name=name or f"Игрок {_next_telegram_id}",
    )
    session.add(user)
    await session.commit()
    return user


async def make_room(session, creator: User, players: list[User], roles_setup: dict | None = None) -> Room:
    """Комната с игроками; все, кроме создателя, сразу готовы."""
    settings = {
        "roles": roles_setup or {"mafia": 1, "detective": 1, "doctor": 1},
        "night_seconds": 60,
        "day_seconds": 60,
        "vote_seconds": 30,
        "start_countdown_seconds": 0,
        "tie_rule": "revote",
        "reveal_roles_on_death": True,
    }
    room = Room(
        creator_id=creator.id,
        name=f"Тестовая комната {creator.id}",
        max_players=max(10, len(players) + 1),
        min_players=4,
        is_private=False,
        status="OPEN",
        settings=settings,
    )
    session.add(room)
    await session.flush()
    for user in players:
        session.add(
            RoomPlayer(
                room_id=room.id,
                user_id=user.id,
                is_ready=(user.id != creator.id),
            )
        )
    await session.commit()

    # Комната загружается с selectin-отношением players
    from bot.database.repositories.rooms import RoomRepository

    return await RoomRepository(session).get(room.id)


async def make_ready(session, room: Room, user: User) -> None:
    from sqlalchemy import update

    await session.execute(
        update(RoomPlayer)
        .where(RoomPlayer.room_id == room.id, RoomPlayer.user_id == user.id)
        .values(is_ready=True)
    )
    await session.commit()
