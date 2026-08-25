"""Точка входа: сборка приложения и запуск long polling.

Запуск: python -m bot.main
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, Update

from bot.config import get_settings
from bot.database.database import create_engine, create_session_factory, dispose_engine, init_db
from bot.handlers import get_root_router
from bot.middlewares import (
    DbSessionMiddleware,
    GroupContextMiddleware,
    MaintenanceMiddleware,
    ServicesMiddleware,
    ThrottlingMiddleware,
    UserMiddleware,
)
from bot.services.app_config import AppConfigService
from bot.services.audit import AuditService
from bot.services.rewards import RewardService
from bot.services.social import SocialService
from bot.services.game_manager import GameManager
from bot.services.groups import GroupService
from bot.services.permissions import PermissionService
from bot.services.rating import RatingService
from bot.services.notifier import TelegramNotifier
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rooms import RoomService
from bot.services.test_game import TestGameManager
from bot.services.timer_manager import TimerManager
from bot.utils.commands_menu import setup_bot_commands
from bot.utils.logging import setup_logging

logger = logging.getLogger(__name__)


class Services:
    """DI-контейнер: доступен хендлерам через data['services']."""

    def __init__(self, session_factory, notifier, timers, phases, games, rooms, app_config,
                 test_games, settings, permissions, groups, audit, rating, maintenance,
                 social, rewards):
        self.session_factory = session_factory
        self.notifier = notifier
        self.timers = timers
        self.phases = phases
        self.games = games
        self.rooms = rooms
        self.app_config = app_config
        self.test_games = test_games
        self.settings = settings
        self.permissions = permissions
        self.groups = groups
        self.audit = audit
        self.rating = rating
        self.maintenance = maintenance
        self.social = social
        self.rewards = rewards


def build_services(bot: Bot, settings) -> tuple[Services, TimerManager]:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    notifier = TelegramNotifier(bot, session_factory)
    timers = TimerManager()
    locks = GameLocks()
    rating = RatingService()
    phases = PhaseManager(
        session_factory, notifier, timers, locks, rating=rating, app_settings=settings
    )
    games = GameManager(session_factory, notifier, phases, locks)
    app_config = AppConfigService(session_factory, settings)
    rooms = RoomService(
        session_factory,
        notifier,
        app_config,
        max_players_limit=settings.max_players_limit,
        min_players_limit=settings.min_players_limit,
    )
    permissions = PermissionService(settings)
    groups_service = GroupService(session_factory, permissions)
    audit = AuditService(session_factory)
    test_games = TestGameManager(session_factory, games, phases, notifier, groups=groups_service)
    maintenance = MaintenanceMiddleware(session_factory)
    social = SocialService(session_factory)
    rewards = RewardService(session_factory)
    services = Services(
        session_factory, notifier, timers, phases, games, rooms, app_config, test_games,
        settings, permissions, groups_service, audit, rating, maintenance, social, rewards,
    )
    services.engine = engine  # для корректного dispose при остановке
    return services, timers


async def create_app() -> tuple[Bot, Dispatcher, Services, TimerManager]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    services, timers = build_services(bot, settings)

    if settings.auto_create_tables:
        await init_db(services.engine)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(get_root_router())

    # Порядок: сессия -> сервисы -> троттлинг -> пользователь -> maintenance -> группа
    dp.update.outer_middleware(DbSessionMiddleware(services.session_factory))
    dp.update.outer_middleware(ServicesMiddleware(services))
    dp.message.middleware(ThrottlingMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware())
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())
    dp.message.middleware(services.maintenance)
    dp.callback_query.middleware(services.maintenance)
    dp.message.middleware(GroupContextMiddleware())
    dp.callback_query.middleware(GroupContextMiddleware())

    @dp.errors()
    async def on_error(event: ErrorEvent, exception: Exception):
        logger.exception("Ошибка обработки апдейта", exc_info=exception)
        update: Update | None = getattr(event, "update", None)
        try:
            if update and update.callback_query:
                await update.callback_query.answer("⚠️ Произошла ошибка. Попробуй ещё раз.", show_alert=True)
            elif update and update.message:
                await update.message.answer("⚠️ Произошла ошибка. Попробуй ещё раз.")
        except Exception:  # pragma: no cover - пользователь мог заблокировать бота
            logger.debug("Не удалось сообщить пользователю об ошибке")
        return True

    @dp.startup()
    async def on_startup() -> None:
        recovered = await services.phases.recover()
        logger.info("Бот запущен. Восстановлено игр: %s", recovered)
        # Автоподсказки команд «/»: базовый список всем, расширенный —
        # глобальной администрации (OWNER_IDS/ADMIN_IDS). UX-only: права
        # проверяются на сервере при каждом вызове команды.
        await setup_bot_commands(bot, settings)

    @dp.shutdown()
    async def on_shutdown() -> None:
        services.test_games.stop_all()
        timers.cancel_all()
        await dispose_engine(services.engine)
        logger.info("Бот остановлен")

    return bot, dp, services, timers


async def main() -> None:
    bot, dp, _services, _timers = await create_app()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
