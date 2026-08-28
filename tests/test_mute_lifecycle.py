"""Regression (раунд 11): mute в игре — lifecycle и Forum Topics.

Два бага, найденных аудитом:

1. «Mute не снимается после игры — бот продолжает удалять сообщения».
   Причина: context_for(chat_id, thread_id=None) матчил ЛЮБУЮ игру чата
   (fallback game_thread_id IS NOT NULL). У сообщений General-темы Telegram
   НЕ присылает message_thread_id, поэтому после завершения партии
   (а группы-форумы по умолчанию создают темы В САМОЙ ГРУППЕ — автозаполнение
   /setup) бот удалял сообщения в основной группе НАВСЕГДА — ложный «вечный
   мут». Фикс: thread_id=None — не игровая тема, контекста нет.

2. «Mute работает в группе, но не работает в Forum Topic».
   Причина: /mute звал restrictChatMember только для основного чата группы;
   форумы партий (game_forum_chat_id/mafia_forum_chat_id) — ОТДЕЛЬНЫЕ чаты,
   ограничение на них не распространяется, а серверная модерация тем
   (enforce_message) об админ-мутах не знает. Фикс: mute/unmute зеркалится
   в форумы этой группы (best-effort, как и основной вызов).

Инварианты (архитектура, ТЗ-15/ТЗ-23): игровая система НИКОГДА не накладывает
Telegram-ограничений — изоляция тем только серверная (delete_message);
админ-мут игре не принадлежит и ею не снимается.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiogram.exceptions import TelegramAPIError

from bot.database.models import Room, RoomPlayer
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.groups import (
    GroupAdminRepository,
    GroupRepository,
    GroupSettingsRepository,
)
from bot.database.repositories.rooms import RoomRepository
from bot.services.app_config import AppConfigService
from bot.services.game_chat import DbForumProvider, GameChatService
from bot.services.game_manager import GameManager
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rating import RatingService
from bot.services.timer_manager import NoopTimerManager
from tests.conftest import SettingsStub, make_user

MAIN_CHAT = -1002000          # группа-форум: /setup автозаполняет форумы собой
OTHER_MAIN = -1003000         # группа B (multi-server)
OTHER_GAME_FORUM = -1009001   # отдельный игровой форум группы B
OTHER_MAFIA_FORUM = -1009002  # отдельный форум мафии группы B


class ForumGateway:
    """Форумный шлюз: темы с инкрементными id, помнит closed/deleted;
    close/delete/send могут падать (проверка «сбой чатов не ломает финал»)."""

    def __init__(self, fail_topic_ops: bool = False) -> None:
        self.closed: set[tuple[int, int]] = set()
        self.deleted: list[tuple[int, int]] = []
        self.sent: list[tuple[int, int | None, str]] = []
        self._next_thread = 5000
        self.fail_topic_ops = fail_topic_ops

    async def create_topic(self, chat_id, name, icon_color=None):
        self._next_thread += 1
        return self._next_thread

    async def close_topic(self, chat_id, thread_id) -> bool:
        if self.fail_topic_ops:
            raise RuntimeError("not enough rights")
        self.closed.add((chat_id, thread_id))
        return True

    async def reopen_topic(self, chat_id, thread_id) -> bool:
        if self.fail_topic_ops:
            raise RuntimeError("not enough rights")
        self.closed.discard((chat_id, thread_id))
        return True

    async def send(self, chat_id, text, thread_id=None) -> bool:
        if self.fail_topic_ops:
            raise RuntimeError("not enough rights")
        self.sent.append((chat_id, thread_id, text))
        return True

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    async def chat_info(self, chat_id) -> dict | None:
        return {"title": f"Форум {chat_id}", "type": "supergroup", "is_forum": True}


class MuteBot:
    """Бот для /mute: логирует restrict_chat_member, падает на выбранных чатах."""

    def __init__(self, fail_chats: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_chats = fail_chats or set()

    async def restrict_chat_member(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        if kwargs["chat_id"] in self.fail_chats:
            raise TelegramAPIError(
                method="restrictChatMember", message="not enough rights"
            )
        return True


async def make_group(session, chat_id: int, game_forum=None, mafia_forum=None):
    """Группа + group_settings с форумами (по умолчанию — автозаполнение собой)."""
    group = await GroupRepository(session).get_or_create(chat_id, f"Группа {chat_id}")
    gs = await GroupSettingsRepository(session).get_or_create(group.id)
    gs.game_forum_chat_id = game_forum if game_forum is not None else chat_id
    gs.mafia_forum_chat_id = mafia_forum if mafia_forum is not None else chat_id
    await session.commit()
    return group


async def group_room(session, group, creator, players, roles_setup=None):
    """Комната ПРИКРЕПЛЁННАЯ к группе (group_id) — партии идут в форумы группы."""
    settings = {
        "roles": roles_setup or {"mafia": 1, "detective": 1, "doctor": 1},
        "night_seconds": 60, "day_seconds": 60, "vote_seconds": 30,
        "start_countdown_seconds": 0, "tie_rule": "revote",
        "reveal_roles_on_death": True,
    }
    room = Room(
        creator_id=creator.id, name=f"Комната группы {group.id}",
        max_players=10, min_players=4, is_private=False,
        status="OPEN", settings=settings, group_id=group.id,
    )
    session.add(room)
    await session.flush()
    for user in players:
        session.add(RoomPlayer(room_id=room.id, user_id=user.id, is_ready=True))
    await session.commit()
    return await RoomRepository(session).get(room.id)


@pytest_asyncio.fixture()
async def env(session_factory, notifier):
    """Хранилище сервисов с per-group форумами из group_settings (как в проде)."""
    gateway = ForumGateway()
    app_config = AppConfigService(session_factory, SettingsStub())
    game_chats = GameChatService(
        session_factory, gateway, notifier,
        forums=DbForumProvider(app_config, SettingsStub()),
    )
    phases = PhaseManager(
        session_factory, notifier, NoopTimerManager(), GameLocks(),
        rating=RatingService(), app_settings=SettingsStub(), game_chats=game_chats,
    )
    games = GameManager(
        session_factory, notifier, phases, GameLocks(), game_chats=game_chats
    )
    container = type("Env", (), {})()
    container.session_factory = session_factory
    container.notifier = notifier
    container.gateway = gateway
    container.game_chats = game_chats
    container.phases = phases
    container.games = games
    yield container


async def start_group_game(env, session, group, users, roles_setup=None):
    """Партия группы: темы создаются в форумах группы автоматически."""
    room = await group_room(session, group, users[0], users, roles_setup)
    res = await env.games.start_game_from_room(room.id, users[0].id)
    assert res.ok, res.message
    async with env.session_factory() as s:
        game_id = (await RoomRepository(s).get(room.id)).game_id
        players = await GamePlayerRepository(s).list_for_game(game_id)
        roles = {p.user_id: p.role for p in players}
        game = await GameRepository(s).get(game_id)
    return game_id, game, roles


async def _refresh(env, game_id):
    async with env.session_factory() as s:
        return await GameRepository(s).get(game_id)


# =====================================================================
# Сценарий 1: lifecycle игровых ограничений
# =====================================================================


class TestGameRestrictionLifecycle:
    async def test_player_gets_game_restriction_during_game(self, env, session):
        """1. Игрок получает игровой мут: мёртвый/ночью писать в тему нельзя."""
        users = [await make_user(session, f"P{i}") for i in range(4)]
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)

        await env.phases.begin_game(game_id)  # ночь 1
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim = next(u for u in users if u.id != mafia_uid)
        await env.games.submit_night_action(game_id, mafia_uid, "kill", victim.id)
        await env.phases.end_night(game_id)  # смерть -> день

        game = await _refresh(env, game_id)
        async with env.session_factory() as s:
            # живой днём пишет в тему партии — можно
            alive = next(u for u in users if u.id not in (victim.id,))
            assert not await env.game_chats.enforce_message(
                s, MAIN_CHAT, game.game_thread_id, alive, 100
            )
            # мёртвый — нельзя (игровое ограничение; только серверное)
            assert await env.game_chats.enforce_message(
                s, MAIN_CHAT, game.game_thread_id, victim, 101
            )
            # ночью (вторая ночь) живому в тему тоже нельзя
        await env.phases.begin_voting(game_id)
        await env.phases.end_voting(game_id)  # -> ночь 2
        game = await _refresh(env, game_id)
        if game.status == "NIGHT":
            async with env.session_factory() as s:
                assert await env.game_chats.enforce_message(
                    s, MAIN_CHAT, game.game_thread_id, alive, 102
                )

    async def test_game_finishes_normally(self, env, session):
        """2. Игра нормально завершается победой (голосование)."""
        users = [await make_user(session, f"L{i}") for i in range(4)]
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)

        await env.phases.begin_game(game_id)  # ночь
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        citizen = next(
            u for u in users
            if u.id != mafia_uid and roles[u.id] not in ("detective", "doctor")
        )
        await env.games.submit_night_action(game_id, mafia_uid, "kill", citizen.id)
        await env.phases.end_night(game_id)  # день
        await env.phases.begin_voting(game_id)
        for u in users:
            if u.id not in (mafia_uid, citizen.id):
                assert (await env.games.cast_vote(game_id, u.id, mafia_uid)).ok
        await env.phases.end_voting(game_id)  # линч мафии -> победа города

        game = await _refresh(env, game_id)
        assert game.status == "ENDED"
        assert game.winner in ("city", "mafia")

    async def test_game_restriction_lifted_after_finalize(self, env, session):
        """3+7. После финала основной чат свободен: General-тема (thread=None)
        больше не «отравлена» игрой — сообщения НЕ удаляются ни у кого."""
        users = [await make_user(session, f"F{i}") for i in range(4)]
        outsider = await make_user(session, "Посторонний")
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)

        # во время игры посторонний в General уже не должен молчать (та же
        # причина бага: thread=None матчил любую игру этого чата)
        async with env.session_factory() as s:
            assert not await env.game_chats.enforce_message(
                s, MAIN_CHAT, None, outsider, 200
            )

        await env.phases.begin_game(game_id)
        await env.phases.force_end(game_id, "тест")

        game = await _refresh(env, game_id)
        assert game.status == "ENDED"
        async with env.session_factory() as s:
            for u in users + [outsider]:
                # game mute -> game finish -> player can write again
                # (основной чат / General: restriction снято финализацией)
                assert not await env.game_chats.enforce_message(
                    s, MAIN_CHAT, None, u, 300 + u.id
                )
            # сама тема партии остаётся историей (read-only) — по дизайну
            assert await env.game_chats.enforce_message(
                s, MAIN_CHAT, game.game_thread_id, users[0], 400
            )
        assert (MAIN_CHAT, game.game_thread_id) in env.gateway.closed

    async def test_admin_mute_not_lifted_by_game_finalize(self, env, session, services):
        """4. Обычный админ-мут игра не снимает: финализация не делает НИ ОДНОГО
        вызова Telegram (у игровой системы нет restrict/unmute вовсе)."""
        users = [await make_user(session, f"M{i}") for i in range(4)]
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)

        bot = MuteBot()
        # «административный мут до игры»: главный чат + зеркала в форумы
        await bot.restrict_chat_member(
            chat_id=MAIN_CHAT, user_id=users[1].telegram_id,
            until_date=1, can_send_messages=False,
        )
        assert len(bot.calls) == 1

        await env.phases.force_end(game_id, "конец")
        game = await _refresh(env, game_id)
        assert game.status == "ENDED"

        # финализация не добавила НИ одного restrict-вызова (в т.ч. unmute)
        assert len(bot.calls) == 1
        assert bot.calls[0]["can_send_messages"] is False
        # у игрового слоя вообще нет Telegram-restrict API (ТЗ-23)
        assert not hasattr(env.gateway, "restrict_chat_member")

    async def test_terminal_paths_all_cleanup(self, env, session):
        """5. Cleanup на КАЖДОМ терминальном пути: темы закрыты, основной чат
        свободен (thread=None не матчится), статус ENDED."""
        users = [await make_user(session, f"T{i}") for i in range(4)]

        # -- a) победа мафии ночью (kill до паритета)
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(
            env, session, group, users, roles_setup={"mafia": 2, "detective": 1, "doctor": 1}
        )
        await env.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim = next(u for u in users if u.id not in (
            uid for uid, r in roles.items() if r == "mafia"
        ))
        await env.games.submit_night_action(game_id, mafia_uid, "kill", victim.id)
        await env.phases.end_night(game_id)  # 2 мафии vs 1 мирный -> победа мафии
        game = await _refresh(env, game_id)
        assert game.status == "ENDED" and game.winner == "mafia"
        assert (MAIN_CHAT, game.game_thread_id) in env.gateway.closed
        assert (MAIN_CHAT, game.mafia_thread_id) in env.gateway.closed
        async with env.session_factory() as s:
            assert not await env.game_chats.enforce_message(
                s, MAIN_CHAT, None, users[0], 500
            )

        # -- b) победа города линчем
        users2 = [await make_user(session, f"V{i}") for i in range(4)]
        game_id, game, roles = await start_group_game(env, session, group, users2)
        await env.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        await env.games.submit_night_action(game_id, mafia_uid, "kill", users2[3].id)
        await env.phases.end_night(game_id)
        await env.phases.begin_voting(game_id)
        for u in users2:
            if u.id not in (mafia_uid, users2[3].id):
                assert (await env.games.cast_vote(game_id, u.id, mafia_uid)).ok
        await env.phases.end_voting(game_id)
        game = await _refresh(env, game_id)
        assert game.status == "ENDED" and game.winner == "city"
        assert (MAIN_CHAT, game.game_thread_id) in env.gateway.closed

        # -- c) выход игроков до паритета
        users3 = [await make_user(session, f"E{i}") for i in range(4)]
        game_id, game, roles = await start_group_game(env, session, group, users3)
        await env.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        # убираем НЕ врача: после ночи 1 мафия vs 2 мирных (игра жива),
        # затем один мирный уходит -> паритет 1:1 -> победа мафии
        victim = next(
            u for u in users3 if roles[u.id] not in ("mafia", "doctor")
        )
        await env.games.submit_night_action(game_id, mafia_uid, "kill", victim.id)
        await env.phases.end_night(game_id)
        game = await _refresh(env, game_id)
        assert game.status != "ENDED"  # ещё не финал: 1 мафия vs 2 мирных
        leaver = next(
            u for u in users3 if u.id != mafia_uid and u.id != victim.id
        )
        # мирный уходит: 1 мафия vs 1 мирный -> паритет -> победа мафии
        assert (await env.games.leave_game(game_id, leaver.id)).ok
        game = await _refresh(env, game_id)
        assert game.status == "ENDED"
        assert (MAIN_CHAT, game.game_thread_id) in env.gateway.closed

        # -- d) принудительная остановка (админ)
        users4 = [await make_user(session, f"S{i}") for i in range(4)]
        game_id, game, roles = await start_group_game(env, session, group, users4)
        await env.phases.begin_game(game_id)
        assert await env.phases.force_end(game_id, "админ")
        game = await _refresh(env, game_id)
        assert game.status == "ENDED"
        assert (MAIN_CHAT, game.game_thread_id) in env.gateway.closed
        assert (MAIN_CHAT, game.mafia_thread_id) in env.gateway.closed

    async def test_finalize_survives_gateway_errors(self, env, session):
        """10b. Ошибки Telegram при закрытии тем не ломают финализацию."""
        users = [await make_user(session, f"X{i}") for i in range(4)]
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)
        env.gateway.fail_topic_ops = True  # все операции с темами падают

        await env.phases.begin_game(game_id)
        assert await env.phases.force_end(game_id, "тест")
        game = await _refresh(env, game_id)
        assert game.status == "ENDED"  # _chats_call изолирует сбои чатов
        async with env.session_factory() as s:
            assert not await env.game_chats.enforce_message(
                s, MAIN_CHAT, None, users[0], 600
            )


# =====================================================================
# Сценарий 2: mute в группе vs Forum Topic
# =====================================================================


class TestMuteForumScope:
    """Модерация /mute: restriction обязано покрывать и темы партий группы."""

    async def _mute(self, services, session, group, mod, target, text="/mute", bot=None):
        from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser

        bot = bot or MuteBot()
        reply = FakeMessage(FakeTgUser(target.telegram_id), "спам")
        msg = FakeMessage(
            FakeTgUser(mod.telegram_id), text,
            chat=FakeChat(group.telegram_chat_id), reply=reply, bot=bot,
        )
        import bot.handlers.groups_admin as ga

        await ga.cmd_mute(
            msg, session=session, group=group, db_user=mod, services=services
        )
        return msg, bot

    async def _mod(self, services, session, group):
        mod = await make_user(session, "Mod")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, mod.id, 1, 0)  # Helper
            await s.commit()
        return mod

    async def test_mute_mirrored_to_forum_chats(self, services, session):
        """Мут группы с ОТДЕЛЬНЫМИ форумами: restrict вызывается для основного
        чата И обоих форумов — в темах партий мут больше не обходится."""
        group = await make_group(
            session, OTHER_MAIN,
            game_forum=OTHER_GAME_FORUM, mafia_forum=OTHER_MAFIA_FORUM,
        )
        mod = await self._mod(services, session, group)
        target = await make_user(session, "Noisy")

        msg, bot = await self._mute(services, session, group, mod, target)
        restricted = {c["chat_id"] for c in bot.calls}
        assert restricted == {OTHER_MAIN, OTHER_GAME_FORUM, OTHER_MAFIA_FORUM}
        assert all(c["can_send_messages"] is False for c in bot.calls)
        assert all(c["user_id"] == target.telegram_id for c in bot.calls)
        assert any("мут на" in t for t in msg.answers)

    async def test_mute_same_chat_forums_single_restrict(self, services, session):
        """Автозаполнение (форумы = сама группа): дублирующих вызовов нет —
        restriction чата уже покрывает все его темы."""
        group = await make_group(session, MAIN_CHAT)  # форумы = сама группа
        mod = await self._mod(services, session, group)
        target = await make_user(session, "Noisy2")

        msg, bot = await self._mute(services, session, group, mod, target)
        assert [c["chat_id"] for c in bot.calls] == [MAIN_CHAT]

    async def test_unmute_lifts_forums_too(self, services, session):
        """/unmute снимает мут со всех чатов: основного и форумов."""
        group = await make_group(
            session, OTHER_MAIN,
            game_forum=OTHER_GAME_FORUM, mafia_forum=OTHER_MAFIA_FORUM,
        )
        mod = await self._mod(services, session, group)
        target = await make_user(session, "Noisy3")

        await self._mute(services, session, group, mod, target)
        msg, bot = await self._mute(
            services, session, group, mod, target, text="/unmute"
        )
        unmutes = [c for c in bot.calls if c.get("can_send_messages") is True]
        assert {c["chat_id"] for c in unmutes} == {
            OTHER_MAIN, OTHER_GAME_FORUM, OTHER_MAFIA_FORUM
        }
        assert any("мут снят" in t for t in msg.answers)

    async def test_mute_group_a_not_affect_group_b(self, services, session):
        """8. Multi-server: мут группы A (её чат + её форумы) не трогает группу B."""
        group_a = await make_group(session, -1004001, -1005001, -1005002)
        group_b = await make_group(session, -1004002, -1005003, -1005004)
        mod = await self._mod(services, session, group_a)
        target = await make_user(session, "Cross")

        msg, bot = await self._mute(services, session, group_a, mod, target)
        restricted = {c["chat_id"] for c in bot.calls}
        assert restricted == {-1004001, -1005001, -1005002}
        assert not restricted & {-1004002, -1005003, -1005004}

    async def test_unmute_forum_api_error_logged_and_not_fatal(
        self, services, session, caplog
    ):
        """10a. Ошибка Telegram при unmute форума: логируется, не ломает ни
        команду, ни основной чат (best-effort, как и сам /mute)."""
        group = await make_group(
            session, OTHER_MAIN,
            game_forum=OTHER_GAME_FORUM, mafia_forum=OTHER_MAFIA_FORUM,
        )
        mod = await self._mod(services, session, group)
        target = await make_user(session, "Noisy4")

        await self._mute(services, session, group, mod, target)
        bot = MuteBot(fail_chats={OTHER_GAME_FORUM})
        msg, bot = await self._mute(
            services, session, group, mod, target, text="/unmute", bot=bot
        )
        # команда завершилась штатно, ответ есть
        assert any("мут снят" in t for t in msg.answers)
        # основной чат и форум мафии размутированы, упавший форум — тоже вызван
        unmuted = [c["chat_id"] for c in bot.calls if c.get("can_send_messages") is True]
        assert set(unmuted) == {OTHER_MAIN, OTHER_GAME_FORUM, OTHER_MAFIA_FORUM}
        # ошибка залогирована, не всплыла
        assert any(
            "unmute" in r.getMessage() and "форуме" in r.getMessage()
            for r in caplog.records
        )


class TestTopicContextBinding:
    """Связка сообщения с группой/игрой: (chat_id, thread_id), БЕЗ подмен."""

    async def test_thread_never_used_as_chat_id(self, env, session):
        """9. thread_id не подменяет chat_id: тема игры A не резолвится в чате B,
        а restrict-вызовы /mute используют только реальные chat_id."""
        users = [await make_user(session, f"B{i}") for i in range(4)]
        group = await make_group(session, MAIN_CHAT)
        game_id, game, roles = await start_group_game(env, session, group, users)

        async with env.session_factory() as s:
            # корректная пара
            found = await env.game_chats.context_for(
                s, MAIN_CHAT, game.game_thread_id
            )
            assert found is not None and found[0].id == game_id
            # та же тема, но ДРУГОЙ чат — подмены chat_id на thread_id нет
            assert await env.game_chats.context_for(
                s, OTHER_MAIN, game.game_thread_id
            ) is None
            # General-тема (thread=None) — не игровая
            assert await env.game_chats.context_for(s, MAIN_CHAT, None) is None
            # посторонняя тема — не игровая
            assert await env.game_chats.context_for(s, MAIN_CHAT, 987654) is None

    async def test_message_binds_to_own_group_game(self, env, session):
        """7. Сообщение в теме связывается с партией СВОЕЙ группы:
        правила игры группы A не действуют в чате группы B."""
        users_a = [await make_user(session, f"A{i}") for i in range(4)]
        users_b = [await make_user(session, f"C{i}") for i in range(4)]
        group_a = await make_group(session, -1006001)
        group_b = await make_group(session, -1006002)

        gid_a, game_a, _ = await start_group_game(env, session, group_a, users_a)
        gid_b, game_b, _ = await start_group_game(env, session, group_b, users_b)
        await env.phases.begin_game(gid_a)  # ночь в игре A
        await env.phases.begin_game(gid_b)  # ночь в игре B

        # игрок игры A пишет в тему игры A ночью — удалено (правило игры A)
        async with env.session_factory() as s:
            assert await env.game_chats.enforce_message(
                s, group_a.telegram_chat_id, game_a.game_thread_id, users_a[0], 700
            )
            # тот же игрок пишет в тему игры B (другая группа!) — он там
            # неучастник: удалено, но по правилу ИГРЫ B, а не A
            assert await env.game_chats.enforce_message(
                s, group_b.telegram_chat_id, game_b.game_thread_id, users_a[0], 701
            )
            # участник игры B в тему игры B ночью — тоже удалено
            assert await env.game_chats.enforce_message(
                s, group_b.telegram_chat_id, game_b.game_thread_id, users_b[0], 702
            )
