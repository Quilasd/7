"""Regression (раунд 12): /unmute — доступность, права, полная пара mute/unmute.

НАЙДЕННАЯ ПРИЧИНА (воспроизведена на настоящем Dispatcher + настоящем aiogram
Bot): cmd_mute звал bot.restrict_chat_member(can_send_messages=...) —
loose-kwargs стиль aiogram 2. В aiogram 3 метод принимает
permissions=ChatPermissions -> TypeError «unexpected keyword argument»
ВНУТРИ хендлера: исключение не TelegramAPIError, не ловится, хендлер
умирает молча — ни restriction, ни ответа, ни записи в аудит. Так вели
себя ОБЕ команды /mute и /unmute в проде (фейки с **kwargs маскировали).

Дополнительно: unmute выдавал только 4 права — по семантике Bot API
пропущенные поля ChatPermissions остаются False, т.е. после «снятия»
игрок оставался без медиа/опросов. Официально: «Pass True for all
permissions to lift restrictions from a user».
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import AuditLog, Base
from bot.database.repositories.groups import (
    GroupAdminRepository,
    GroupRepository,
    GroupSettingsRepository,
)
from bot.handlers import get_root_router
from bot.middlewares import DbSessionMiddleware, GroupContextMiddleware, ServicesMiddleware
from bot.middlewares.game_chat_guard import GameChatGuardMiddleware
from bot.middlewares.throttling import ThrottlingMiddleware
from bot.middlewares.user import UserMiddleware
from bot.services.app_config import AppConfigService
from bot.services.audit import AuditService
from bot.services.groups import GroupService
from bot.services.notifier import FakeNotifier
from bot.services.permissions import PermissionService
from bot.utils.command_registry import ADMIN_COMMANDS, admin_help_text
from tests.conftest import SettingsStub, make_user
from tests.test_command_registry import collect_registered_commands, registry_names

GROUP_CHAT = -1008001
GAME_FORUM = -1008101
MAFIA_FORUM = -1008102
OTHER_GROUP_CHAT = -1008002
OTHER_FORUM = -1008201

# полный «участниковый» набор после unmute (без админских)
MEMBER_PERMS = {
    "can_send_messages", "can_send_polls", "can_send_other_messages",
    "can_add_web_page_previews", "can_send_audios", "can_send_documents",
    "can_send_photos", "can_send_videos", "can_send_video_notes",
    "can_send_voice_notes", "can_invite_users",
}
ADMIN_ONLY_PERMS = ("can_change_info", "can_pin_messages", "can_manage_topics")


class LiveBot(Bot):
    """Настоящий aiogram Bot без сети: перехват __call__ ПОСЛЕ того, как
    типизированный метод уже провалидировал аргументы. Любое несовпадение
    сигнатуры (loose-kwargs и т.п.) падает здесь TypeError — как в проде."""

    def __init__(self) -> None:
        super().__init__(token="1:test")
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method, request_timeout=None):
        self.calls.append(
            (method.__api_method__, dict(method.model_dump(exclude_none=True)))
        )
        return True

    def restrict_calls(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "restrictChatMember"]

    def sent_texts(self) -> list[str]:
        return [kw.get("text", "") for name, kw in self.calls if name == "sendMessage"]


def _update(text: str, from_id: int, reply_to: int | None = None,
            chat: int = GROUP_CHAT) -> Update:
    msg: dict = {
        "message_id": 100,
        "date": 1,
        "chat": {"id": chat, "type": "supergroup", "title": "Группа"},
        "from": {"id": from_id, "is_bot": False, "first_name": "U"},
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = {
            "message_id": 90,
            "date": 1,
            "chat": {"id": chat, "type": "supergroup"},
            "from": {"id": reply_to, "is_bot": False, "first_name": "T"},
            "text": "x",
        }
    return Update.model_validate({"update_id": 1, "message": msg})


def _detach_handler_routers() -> None:
    """Роутеры хендлеров — модульные синглтоны aiogram, и дерево мог собрать
    другой тест (create_app в test_app_smoke), а роутер aiogram имеет только
    одного родителя. Отвязываем, чтобы собрать СВОЁ дерево; teardown отвязывает
    снова — последующие тесты могут собрать дерево заново (get_root_router) или
    обходить его по глобальным роутерам модулей (test_command_registry)."""
    import bot.handlers as handlers_pkg

    for name in (
        "admin", "cmdhelp", "owner", "testgame", "rewards", "groups_admin",
        "ratings", "setup", "start", "profile", "social", "history",
        "rooms", "game", "voting", "game_chats",
    ):
        router = getattr(getattr(handlers_pkg, name, None), "router", None)
        if router is not None and getattr(router, "_parent_router", None) is not None:
            router._parent_router = None


@pytest_asyncio.fixture(scope="module")
async def live_env():
    """Один Dispatcher на модуль (роутеры aiogram присоединяются единожды):
    настоящий root router + полный middleware-цепочка как в main.py,
    собственная БД и контейнер сервисов."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    settings = SettingsStub()
    permissions = PermissionService(settings)
    services = SimpleNamespace(
        session_factory=sf,
        notifier=FakeNotifier(),
        settings=settings,
        permissions=permissions,
        groups=GroupService(sf, permissions),
        audit=AuditService(sf),
        app_config=AppConfigService(sf, settings),
    )

    # группы: A с отдельными форумами, B — для проверки изоляции
    async with sf() as s:
        group = await GroupRepository(s).get_or_create(GROUP_CHAT, "Группа A")
        other = await GroupRepository(s).get_or_create(OTHER_GROUP_CHAT, "Группа B")
        gs = await GroupSettingsRepository(s).get_or_create(group.id)
        gs.game_forum_chat_id = GAME_FORUM
        gs.mafia_forum_chat_id = MAFIA_FORUM
        gs_b = await GroupSettingsRepository(s).get_or_create(other.id)
        gs_b.game_forum_chat_id = OTHER_FORUM
        await s.commit()

    _detach_handler_routers()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(get_root_router())
    dp.update.outer_middleware(DbSessionMiddleware(sf))
    dp.update.outer_middleware(ServicesMiddleware(services))
    dp.message.middleware(ThrottlingMiddleware(rate=0))
    dp.message.middleware(UserMiddleware())
    dp.message.middleware(GroupContextMiddleware())
    dp.message.middleware(GameChatGuardMiddleware())

    bot = LiveBot()
    try:
        yield SimpleNamespace(
            bot=bot, dp=dp, services=services, session_factory=sf,
            group=group, other=other,
        )
    finally:
        _detach_handler_routers()
        await engine.dispose()


@pytest_asyncio.fixture()
async def live(live_env):
    """Свежие модератор (Helper, Lv.1) и цель на каждый тест."""
    async with live_env.session_factory() as s:
        mod = await make_user(s, "mod")
        target = await make_user(s, "target")
        await GroupAdminRepository(s).set_level(live_env.group.id, mod.id, 1, 0)
        await s.commit()
    live_env.bot.calls.clear()
    return SimpleNamespace(
        bot=live_env.bot, dp=live_env.dp, mod=mod, target=target,
        group=live_env.group, other=live_env.other,
        session_factory=live_env.session_factory,
    )


class TestUnmuteRegistration:
    async def test_unmute_is_registered_in_real_router(self):
        """1. /unmute реально зарегистрирован: настоящие Command-фильтры
        aiogram содержат команду (не только запись в реестре справки)."""
        registered = collect_registered_commands()
        assert "unmute" in registered
        assert "mute" in registered

    async def test_registry_and_acmdhelp_match_reality(self):
        """9. /acmdhelp и Command Registry соответствуют факту: реестр
        знает unmute с тем же правом, что и хендлер; справка Lv.1 его
        показывает; расхождений реестра с роутерами нет."""
        meta = next(m for m in ADMIN_COMMANDS if m.command == "unmute")
        assert meta.permission is not None
        assert meta.permission.value == "MUTE_PLAYER"  # тот же уровень, что у /mute

        registered = collect_registered_commands()
        assert registered == registry_names()  # двусторонняя сверка

        text = admin_help_text(level=1, is_global=False, in_group=True)
        assert "/unmute" in text and "/mute" in text


class TestUnmutePermissions:
    async def test_unmute_allowed_for_helper_denied_for_player(self, live):
        """2+5. /unmute доступен Helper (Lv.1, MUTE_PLAYER); обычному игроку —
        отказ, restriction не вызывается."""
        # игрок без прав
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.target.telegram_id,
                              reply_to=live.mod.telegram_id)
        )
        assert live.bot.restrict_calls() == []
        assert any("MUTE_PLAYER" in t for t in live.bot.sent_texts())

        # Helper
        live.bot.calls.clear()
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.mod.telegram_id,
                              reply_to=live.target.telegram_id)
        )
        assert len(live.bot.restrict_calls()) >= 1
        assert any("мут снят" in t for t in live.bot.sent_texts())


class TestMuteUnmutePair:
    async def test_mute_applies_full_restriction(self, live):
        """3. /mute реально ограничивает: restrictChatMember с полным запретом
        отправки (текст + медиа + опросы), срок задан, чаты — группа и форумы."""
        await live.dp.feed_update(
            live.bot, _update(f"/mute {live.target.telegram_id} 30m",
                              live.mod.telegram_id)
        )
        calls = live.bot.restrict_calls()
        assert {c["chat_id"] for c in calls} == {GROUP_CHAT, GAME_FORUM, MAFIA_FORUM}
        for c in calls:
            assert c["user_id"] == live.target.telegram_id
            assert c["until_date"] is not None
            perms = c["permissions"]
            assert perms["can_send_messages"] is False
            assert perms["can_send_polls"] is False
            assert perms["can_send_photos"] is False
            assert perms["can_send_videos"] is False
        assert any("мут на" in t for t in live.bot.sent_texts())

    async def test_unmute_fully_restores_permissions(self, live):
        """4+8. /mute -> restriction -> /unmute -> права восстановлены:
        полный участниковый набор True (официальная семантика «pass True for
        all permissions to lift restrictions»), без админских прав."""
        await live.dp.feed_update(
            live.bot, _update(f"/mute {live.target.telegram_id}", live.mod.telegram_id)
        )
        assert live.bot.restrict_calls(), "мут не применился"

        live.bot.calls.clear()
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.mod.telegram_id,
                              reply_to=live.target.telegram_id)
        )
        calls = live.bot.restrict_calls()
        assert {c["chat_id"] for c in calls} == {GROUP_CHAT, GAME_FORUM, MAFIA_FORUM}
        for c in calls:
            assert c["user_id"] == live.target.telegram_id
            perms = c["permissions"]
            # участник снова может писать — и текст, и медиа, и опросы
            for key in MEMBER_PERMS:
                assert perms.get(key) is True, (key, perms)
            # админские права не выдаются
            for key in ADMIN_ONLY_PERMS:
                assert not perms.get(key), (key, perms)
            # срок не задан — ограничение не «истекает», а снято
            assert c.get("until_date") is None
        assert any("мут снят" in t for t in live.bot.sent_texts())

    async def test_pair_ids_and_audit(self, live):
        """7. ID без смешивания: Telegram API получает telegram_id (не DB id);
        в аудит пишутся DB users.id; chat_id — реальный Telegram chat."""
        await live.dp.feed_update(
            live.bot, _update(f"/mute {live.target.telegram_id} 1h",
                              live.mod.telegram_id)
        )
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.mod.telegram_id,
                              reply_to=live.target.telegram_id)
        )
        # Telegram-слой: только telegram_id
        for c in live.bot.restrict_calls():
            assert c["user_id"] == live.target.telegram_id
            assert c["user_id"] != live.target.id  # telegram_id != users.id
        # DB-слой: аудит с внутренними id
        async with live.session_factory() as s:
            rows = list((await s.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == live.mod.id,
                    AuditLog.action.in_(("mute", "unmute")),
                )
            )).scalars().all())
        assert {r.action for r in rows} == {"mute", "unmute"}
        for r in rows:
            assert r.actor_id == live.mod.id
            assert r.target_id == live.target.id
            assert r.group_id == live.group.id
            assert r.actor_id != live.mod.telegram_id

    async def test_unmute_by_id_and_by_username(self, live):
        """7. Цель: ID, @username и reply (UserLookupService) — все пути."""
        async with live.session_factory() as s:
            from bot.database.repositories.users import UserRepository

            fresh = await UserRepository(s).get_by_telegram_id(live.target.telegram_id)
            fresh.username = "targetuser"
            await s.commit()

        for text in (
            f"/unmute {live.target.telegram_id}",
            "/unmute @targetuser",
        ):
            live.bot.calls.clear()
            await live.dp.feed_update(live.bot, _update(text, live.mod.telegram_id))
            assert any(
                c["user_id"] == live.target.telegram_id
                for c in live.bot.restrict_calls()
            ), text

    async def test_unmute_only_current_group(self, live):
        """6. Размут только в контексте ТЕКУЩЕЙ группы: чаты другой группы
        не затрагиваются (права группы A не действуют в B)."""
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.mod.telegram_id,
                              reply_to=live.target.telegram_id)
        )
        touched = {c["chat_id"] for c in live.bot.restrict_calls()}
        assert touched == {GROUP_CHAT, GAME_FORUM, MAFIA_FORUM}
        assert OTHER_GROUP_CHAT not in touched
        assert OTHER_FORUM not in touched

    async def test_unmute_without_target_usage_hint(self, live):
        """/unmute без цели не падает: подсказка как указать игрока."""
        await live.dp.feed_update(live.bot, _update("/unmute", live.mod.telegram_id))
        assert live.bot.restrict_calls() == []
        assert any("Укажи игрока" in t for t in live.bot.sent_texts())


class TestRealApiSignature:
    async def test_restrict_calls_match_real_aiogram_signature(self, live):
        """Защита от маскировки фейками: каждый restrict-вызов хендлера
        биндится к сигнатуре НАСТОЯЩЕГО Bot.restrict_chat_member
        (регрессия: can_send_messages=... валился с TypeError в проде)."""
        import inspect

        await live.dp.feed_update(
            live.bot, _update(f"/mute {live.target.telegram_id}", live.mod.telegram_id)
        )
        await live.dp.feed_update(
            live.bot, _update("/unmute", live.mod.telegram_id,
                              reply_to=live.target.telegram_id)
        )
        sig = inspect.signature(Bot.restrict_chat_member)
        for name, kwargs in live.bot.calls:
            if name != "restrictChatMember":
                continue
            bound = sig.bind(None, **kwargs)  # TypeError при несовпадении
            assert bound.arguments["permissions"] is not None
