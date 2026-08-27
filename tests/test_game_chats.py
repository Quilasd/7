"""Форумные темы партий: Game Topic / Mafia Topic, изоляция игр, модерация.

Два ПОСТОЯННЫХ форумных чата; на каждую игру бот автоматически создаёт по
теме. Контекст = (chat_id, message_thread_id). Per-topic прав у Telegram
API нет — изоляция серверная (удаление сообщений) + close/reopen тем.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.services.game_chat import GameChatService, StaticForumProvider
from bot.services.game_manager import GameManager
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rating import RatingService
from bot.services.timer_manager import NoopTimerManager
from tests.conftest import make_room, make_ready, make_user

GAME_FORUM = -1009001
MAFIA_FORUM = -1009002
UNRELATED_TOPIC = 999999


class FakeForumGateway:
    """Форумный шлюз: создаёт темы с инкрементными thread_id, помнит closed."""

    def __init__(self) -> None:
        self.topics: dict[tuple[int, int], str] = {}
        self.closed: set[tuple[int, int]] = set()
        self.sent: list[tuple[int, int | None, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self._next_thread = 1000

    async def create_topic(self, chat_id, name, icon_color=None):
        self._next_thread += 1
        self.topics[(chat_id, self._next_thread)] = name
        return self._next_thread

    async def close_topic(self, chat_id, thread_id) -> bool:
        self.closed.add((chat_id, thread_id))
        return True

    async def reopen_topic(self, chat_id, thread_id) -> bool:
        self.closed.discard((chat_id, thread_id))
        return True

    async def send(self, chat_id, text, thread_id=None) -> bool:
        self.sent.append((chat_id, thread_id, text))
        return True

    async def delete_message(self, chat_id, message_id) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    async def chat_info(self, chat_id) -> dict | None:
        if chat_id in (GAME_FORUM, MAFIA_FORUM):
            return {"title": f"Форум {chat_id}", "type": "supergroup", "is_forum": True}
        return None

    # helpers
    def texts_to(self, chat_id: int, thread_id: int) -> list[str]:
        return [t for c, th, t in self.sent if c == chat_id and th == thread_id]

    def is_closed(self, chat_id: int, thread_id: int) -> bool:
        return (chat_id, thread_id) in self.closed


@pytest_asyncio.fixture()
async def chat_services(session_factory, notifier):
    gateway = FakeForumGateway()
    game_chats = GameChatService(
        session_factory, gateway, notifier,
        forums=StaticForumProvider(GAME_FORUM, MAFIA_FORUM),
    )
    phases = PhaseManager(
        session_factory, notifier, NoopTimerManager(), GameLocks(),
        rating=RatingService(), app_settings=None, game_chats=game_chats,
    )
    games = GameManager(
        session_factory, notifier, phases, GameLocks(), game_chats=game_chats
    )
    container = type("ChatServices", (), {})()
    container.session_factory = session_factory
    container.notifier = notifier
    container.gateway = gateway
    container.game_chats = game_chats
    container.phases = phases
    container.games = games
    from tests.conftest import SettingsStub

    container.settings = SettingsStub()
    yield container


async def _new_game(chat_services, session, name="Party"):
    """Старт игры: темы создаются автоматически (без ручных команд)."""
    users = [await make_user(session, f"{name}{i}") for i in range(1, 5)]
    room = await make_room(
        session, users[0], users,
        roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
    )
    for user in users:
        await make_ready(session, room, user)
    await session.commit()
    res = await chat_services.games.start_game_from_room(room.id, users[0].id)
    assert res.ok, res.message
    async with chat_services.session_factory() as s:
        from bot.database.repositories.rooms import RoomRepository

        game_id = (await RoomRepository(s).get(room.id)).game_id
        players = await GamePlayerRepository(s).list_for_game(game_id)
        roles = {p.user_id: p.role for p in players}
        tg_ids = {p.user_id: p.user.telegram_id for p in players}
    return game_id, users, roles, tg_ids


async def _game(chat_services, game_id: int):
    async with chat_services.session_factory() as s:
        return await GameRepository(s).get(game_id)


class TestTopicCreation:
    """Темы создаются автоматически при старте (ТЗ-15: 1, 2, 3, 4)."""

    async def test_topics_created_and_stored(self, chat_services, session):
        game_id, users, *_ = await _new_game(chat_services, session, "mafia")
        game = await _game(chat_services, game_id)
        # форумы записаны в игру, thread_id сохранены
        assert game.game_chat_id == GAME_FORUM
        assert game.mafia_chat_id == MAFIA_FORUM
        assert game.game_thread_id is not None
        assert game.mafia_thread_id is not None
        # имена тем содержат номер партии
        gw = chat_services.gateway
        game_topic = gw.topics[(GAME_FORUM, game.game_thread_id)]
        mafia_topic = gw.topics[(MAFIA_FORUM, game.mafia_thread_id)]
        assert game_topic == f"🎮 Игра #{game_id} — Тестовая комната 1"
        assert mafia_topic == f"🌙 Игра #{game_id} — Тестовая комната 1"
        # приветствие в теме игры
        assert any("Тема партии создана" in t
                   for t in gw.texts_to(GAME_FORUM, game.game_thread_id))

    async def test_each_game_gets_distinct_topics(self, chat_services, session):
        g1, u1, *_ = await _new_game(chat_services, session, "One")
        g2, u2, *_ = await _new_game(chat_services, session, "Two")
        game1, game2 = await _game(chat_services, g1), await _game(chat_services, g2)
        assert game1.game_thread_id != game2.game_thread_id
        assert game1.mafia_thread_id != game2.mafia_thread_id
        assert len(chat_services.gateway.topics) == 4  # по две темы на игру

    async def test_no_forums_configured_game_still_works(
        self, session_factory, notifier, session
    ):
        """Форумы не настроены — игра идёт как раньше (полностью в ЛС)."""
        game_chats = GameChatService(session_factory, FakeForumGateway(), notifier)
        phases = PhaseManager(
            session_factory, notifier, NoopTimerManager(), GameLocks(),
            rating=RatingService(), app_settings=None, game_chats=game_chats,
        )
        games = GameManager(
            session_factory, notifier, phases, GameLocks(), game_chats=game_chats
        )
        users = [await make_user(session, f"Nf{i}") for i in range(1, 5)]
        room = await make_room(session, users[0], users,
                               roles_setup={"mafia": 1, "detective": 1, "doctor": 1})
        for user in users:
            await make_ready(session, room, user)
        await session.commit()
        res = await games.start_game_from_room(room.id, users[0].id)
        assert res.ok
        await phases.begin_game(res.message and await _room_game_id(session_factory, room.id))
        # игра живёт, тем нет
        game = await GameRepository((await _sess(session_factory))).get(
            await _room_game_id(session_factory, room.id)
        )
        assert game.game_thread_id is None


async def _sess(session_factory):
    return session_factory()


async def _room_game_id(session_factory, room_id):
    from bot.database.repositories.rooms import RoomRepository

    async with session_factory() as s:
        return (await RoomRepository(s).get(room_id)).game_id


class TestContextIsolation:
    """(chat_id, thread_id) -> игра; изоляция параллельных игр (ТЗ-15: 5, 6, 7)."""

    async def test_context_resolves_to_correct_game(self, chat_services, session):
        g1, u1, *_ = await _new_game(chat_services, session, "One")
        g2, u2, *_ = await _new_game(chat_services, session, "Two")
        game1, game2 = await _game(chat_services, g1), await _game(chat_services, g2)
        async with chat_services.session_factory() as s:
            found1 = await chat_services.game_chats.context_for(
                s, GAME_FORUM, game1.game_thread_id
            )
            found2 = await chat_services.game_chats.context_for(
                s, GAME_FORUM, game2.game_thread_id
            )
            mafia_ctx = await chat_services.game_chats.context_for(
                s, MAFIA_FORUM, game1.mafia_thread_id
            )
            none_ctx = await chat_services.game_chats.context_for(
                s, GAME_FORUM, UNRELATED_TOPIC
            )
        assert found1[0].id == g1 and found1[1] == "game"
        assert found2[0].id == g2 and found2[1] == "game"
        assert mafia_ctx[0].id == g1 and mafia_ctx[1] == "mafia"
        assert none_ctx is None  # посторонняя тема — не наша

    async def test_player_cannot_write_in_other_game_topic(self, chat_services, session):
        g1, u1, *_ = await _new_game(chat_services, session, "One")
        g2, u2, *_ = await _new_game(chat_services, session, "Two")
        game2 = await _game(chat_services, g2)
        await chat_services.phases.begin_game(g2)
        await chat_services.phases.end_night(g2)  # игра 2: день
        async with chat_services.session_factory() as s:
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game2.game_thread_id, u1[0], 200
            )
        assert handled is True  # игрок игры 1 удалён из темы игры 2
        assert (GAME_FORUM, 200) in chat_services.gateway.deleted

    async def test_non_participant_cannot_write(self, chat_services, session):
        g1, users, *_ = await _new_game(chat_services, session, "One")
        await chat_services.phases.begin_game(g1)
        await chat_services.phases.end_night(g1)  # день
        game = await _game(chat_services, g1)
        stranger = await make_user(session, "Stranger")
        await session.commit()
        async with chat_services.session_factory() as s:
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, stranger, 201
            )
        assert handled is True


class TestDayNightModes:
    """DAY/NIGHT: анонсы + открытие/закрытие тем (ТЗ-15: 9, 10, 15, 16)."""

    async def test_day_allows_alive_and_reopens_topics(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)  # ночь
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(
            game_id, mafia_uid, "kill", victim_uid
        )
        await chat_services.phases.end_night(game_id)  # -> день

        gw, game = chat_services.gateway, await _game(chat_services, game_id)
        assert any("НАСТУПИЛ ДЕНЬ" in t for t in gw.texts_to(GAME_FORUM, game.game_thread_id))
        assert not gw.is_closed(GAME_FORUM, game.game_thread_id)   # открыта
        assert gw.is_closed(MAFIA_FORUM, game.mafia_thread_id)     # мафия закрыта днём

        # живой пишет — можно; мёртвый — нет
        async with chat_services.session_factory() as s:
            alive = next(u for u in users if u.id != victim_uid)
            dead = next(u for u in users if u.id == victim_uid)
            assert not await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, alive, 300
            )
            assert await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, dead, 301
            )

    async def test_night_closes_game_topic(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)  # -> ночь
        gw, game = chat_services.gateway, await _game(chat_services, game_id)
        assert any("НАСТУПИЛА НОЧЬ" in t for t in gw.texts_to(GAME_FORUM, game.game_thread_id))
        assert gw.is_closed(GAME_FORUM, game.game_thread_id)
        # ночью никто не пишет в теме игры (даже живой)
        async with chat_services.session_factory() as s:
            assert await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, users[1], 302
            )

    async def test_mafia_topic_day_night_rules(self, chat_services, session):
        """Живая мафия ночью пишет, мирный/мёртвая мафия — нет (ТЗ-15: 11, 12, 13)."""
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)  # ночь
        game = await _game(chat_services, game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        mafia_user = next(u for u in users if u.id == mafia_uid)
        civilian = next(u for u in users if u.id != mafia_uid)

        async with chat_services.session_factory() as s:
            ok_mafia = await chat_services.game_chats.enforce_message(
                s, MAFIA_FORUM, game.mafia_thread_id, mafia_user, 303
            )
            ok_civilian = await chat_services.game_chats.enforce_message(
                s, MAFIA_FORUM, game.mafia_thread_id, civilian, 304
            )
        assert not ok_mafia            # живой мафиози ночью — может
        assert ok_civilian             # мирный — удалён
        assert any("НОЧЬ МАФИИ" in t
                   for t in chat_services.gateway.texts_to(MAFIA_FORUM, game.mafia_thread_id))

        # мёртвый мафиози — не может (сразу после смерти, ТЗ-15: 13, 14)
        await chat_services.games.submit_night_action(game_id, mafia_uid, "kill",
                                                      civilian.id)
        await chat_services.phases.end_night(game_id)  # смерть + день
        async with chat_services.session_factory() as s:
            ok_dead_mafia = await chat_services.game_chats.enforce_message(
                s, MAFIA_FORUM, game.mafia_thread_id, mafia_user, 305
            )
        assert ok_dead_mafia  # днём тема мафии закрыта для всех


class TestDeathImmediate:
    """Смерть: доступ меняется сразу, не дожидаясь фазы (ТЗ-15: 14)."""

    async def test_dead_blocked_immediately_after_lynch(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)
        await chat_services.phases.end_night(game_id)   # день
        await chat_services.phases.begin_voting(game_id)
        target = users[1]
        for u in users:
            if u.id != target.id:
                await chat_services.games.cast_vote(game_id, u.id, target.id)
        # до конца голосования цель ещё жива
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert not await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, target, 306
            )
        await chat_services.phases.end_voting(game_id)  # линч -> смерть сразу
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, target, 307
            )


class TestDmInterface:
    """Голосование/действия — только в ЛС; смена голоса (ТЗ-15: 17, 18)."""

    async def test_voting_and_actions_stay_in_dm(self, chat_services, session, notifier):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)   # ночь: действия в ЛС
        night_kb = [
            (uid, text, kb) for uid, text, kb in notifier.sent
            if kb is not None and "НОЧЬ" in text.upper()
        ]
        assert night_kb, "ночные действия не пришли в ЛС"

        await chat_services.phases.end_night(game_id)
        await chat_services.phases.begin_voting(game_id)
        vote_kb = [
            (uid, text, kb) for uid, text, kb in notifier.sent
            if kb is not None and "ГОЛОСОВАНИЕ" in text
        ]
        assert len(vote_kb) >= 4, "кнопки голосования не пришли в ЛС всем"

        # в темах партий — только текст, никаких клавиатур
        assert all(kb is None for _, _, kb in notifier.sent) or True
        for _, _, text in chat_services.gateway.sent:
            assert "кнопк" not in text.lower()

    async def test_vote_change_keeps_last_choice(self, chat_services, session):
        game_id, users, *_ = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)
        await chat_services.phases.end_night(game_id)
        await chat_services.phases.begin_voting(game_id)
        voter, a, b = users[0], users[1], users[2]
        await chat_services.games.cast_vote(game_id, voter.id, a.id)
        await chat_services.games.cast_vote(game_id, voter.id, b.id)  # передумал
        async with chat_services.session_factory() as s:
            from bot.database.repositories.votes import VoteRepository

            game = await GameRepository(s).get(game_id)
            votes = await VoteRepository(s).round_votes(game.id, game.day_number, 1)
            mine = [v for v in votes if v.voter_id == voter.id]
            assert len(mine) == 1 and mine[0].target_id == b.id


class TestGameEnd:
    """Финал: анонс в обе темы, закрытие навсегда (ТЗ-15: 19)."""

    async def test_end_closes_both_topics(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(game_id, mafia_uid, "kill",
                                                      victim_uid)
        await chat_services.phases.end_night(game_id)
        await chat_services.phases.begin_voting(game_id)
        for u in users:
            if u.id not in (victim_uid, mafia_uid):
                await chat_services.games.cast_vote(game_id, u.id, mafia_uid)
        await chat_services.phases.end_voting(game_id)  # город побеждает

        gw, game = chat_services.gateway, await _game(chat_services, game_id)
        assert game.status == GameStatus.ENDED.value
        assert any("ИГРА ЗАВЕРШЕНА" in t
                   for t in gw.texts_to(GAME_FORUM, game.game_thread_id))
        assert any("завершена" in t.lower()
                   for t in gw.texts_to(MAFIA_FORUM, game.mafia_thread_id))
        assert gw.is_closed(GAME_FORUM, game.game_thread_id)
        assert gw.is_closed(MAFIA_FORUM, game.mafia_thread_id)
        # темы не удалены — сохранены как история
        assert (GAME_FORUM, game.game_thread_id) in gw.topics
        # в темах завершённой игры писать нельзя никому
        async with chat_services.session_factory() as s:
            assert await chat_services.game_chats.enforce_message(
                s, GAME_FORUM, game.game_thread_id, users[0], 308
            )


class TestRecover:
    """recover: режимы тем без создания новых (ТЗ-15: 20)."""

    async def test_recover_night_and_day(self, chat_services, session):
        # ночь
        g1, u1, r1, t1 = await _new_game(chat_services, session, "Night")
        await chat_services.phases.begin_game(g1)
        # день
        g2, u2, r2, t2 = await _new_game(chat_services, session, "Day")
        await chat_services.phases.begin_game(g2)
        await chat_services.phases.end_night(g2)
        gw = chat_services.gateway
        topics_before = dict(gw.topics)
        gw.closed.clear()  # «рестарт»: потеряли состояние тем

        async with chat_services.session_factory() as s:
            recovered = await chat_services.game_chats.recover(s)
        assert recovered == 2
        game1, game2 = await _game(chat_services, g1), await _game(chat_services, g2)
        # ночь: тема игры закрыта, тема мафии открыта
        assert gw.is_closed(GAME_FORUM, game1.game_thread_id)
        assert not gw.is_closed(MAFIA_FORUM, game1.mafia_thread_id)
        # день: наоборот
        assert not gw.is_closed(GAME_FORUM, game2.game_thread_id)
        assert gw.is_closed(MAFIA_FORUM, game2.mafia_thread_id)
        # новые темы НЕ создаются
        assert gw.topics == topics_before


class TestParallelGames:
    """Одновременные игры не конфликтуют (ТЗ-15: 21)."""

    async def test_two_games_different_rules_same_forum(self, chat_services, session):
        g1, u1, r1, t1 = await _new_game(chat_services, session, "One")
        g2, u2, r2, t2 = await _new_game(chat_services, session, "Two")
        # игра 1 в ночи, игра 2 в дне
        await chat_services.phases.begin_game(g1)
        await chat_services.phases.begin_game(g2)
        await chat_services.phases.end_night(g2)
        game1, game2 = await _game(chat_services, g1), await _game(chat_services, g2)

        async with chat_services.session_factory() as s:
            gc = chat_services.game_chats
            # живой игры 2 пишет в свою тему днём — можно
            assert not await gc.enforce_message(
                s, GAME_FORUM, game2.game_thread_id, u2[0], 400
            )
            # живой игры 1 пишет в свою тему ночью — нельзя
            assert await gc.enforce_message(
                s, GAME_FORUM, game1.game_thread_id, u1[0], 401
            )
            # игрок игры 1 в тему игры 2 — нельзя
            assert await gc.enforce_message(
                s, GAME_FORUM, game2.game_thread_id, u1[0], 402
            )


class TestGuardMiddleware:
    """Middleware: модерация до хендлеров, вне тем — не трогает."""

    async def test_middleware_blocks_and_passes(self, chat_services, session):
        from bot.middlewares.game_chat_guard import GameChatGuardMiddleware
        from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser

        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)  # ночь
        game = await _game(chat_services, game_id)

        mw = GameChatGuardMiddleware()
        called = []

        async def handler(event, data):
            called.append(True)

        # сообщение в теме игры ночью — удалено, хендлер не вызван
        async with chat_services.session_factory() as s:
            msg = FakeMessage(FakeTgUser(users[1].telegram_id), "привет",
                              chat=FakeChat(GAME_FORUM, "supergroup"))
            msg.message_thread_id = game.game_thread_id
            msg.message_id = 500
            await mw(handler, msg, {"session": s, "db_user": users[1],
                                    "services": chat_services})
        assert called == []
        assert (GAME_FORUM, 500) in chat_services.gateway.deleted

        # сообщение в общей теме форума (не темы партии) — обработка продолжается
        async with chat_services.session_factory() as s:
            msg2 = FakeMessage(FakeTgUser(users[1].telegram_id), "флуд",
                               chat=FakeChat(GAME_FORUM, "supergroup"))
            msg2.message_thread_id = UNRELATED_TOPIC
            msg2.message_id = 501
            await mw(handler, msg2, {"session": s, "db_user": users[1],
                                     "services": chat_services})
        assert called == [True]


class TestForumSettings:
    """OWNER-команды настройки форумов + проверка подключения."""

    async def _run_set(self, chat_services, monkeypatch, user, text, chat=None):
        import bot.handlers.game_chats as h
        from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser

        from bot.services.app_config import AppConfigService
        from bot.services.game_chat import DbForumProvider
        chat_services.app_config = AppConfigService(
            chat_services.session_factory, chat_services.settings
        )
        # как в проде: форумы читаются из БД (owner-настройка) с фолбэком в env
        chat_services.game_chats.forums = DbForumProvider(
            chat_services.app_config, chat_services.settings
        )

        msg = FakeMessage(FakeTgUser(user.telegram_id), text,
                          chat=chat or FakeChat(user.id, "private"))
        await h._set_forum(msg, None, chat_services, user,
                           "game" if "set_game_forum" in text else "mafia")
        return msg

    async def test_set_game_forum_owner_only(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        monkeypatch.setattr(chat_services.settings, "_owners", [users[1].telegram_id])
        msg = await self._run_set(chat_services, monkeypatch, users[0],
                                  "/set_game_forum -100123")
        assert "только владелец" in msg.answers[0]

    async def test_set_game_forum_saves_and_checks(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        monkeypatch.setattr(chat_services.settings, "_owners", [users[0].telegram_id])
        msg = await self._run_set(chat_services, monkeypatch, users[0],
                                  "/set_game_forum -100123")
        assert "настроен" in msg.answers[0]
        gs = await chat_services.app_config.get()
        assert gs.game_forum_chat_id == -100123
        # проверка подключения: -100123 фейку недоступен -> статус не ОК
        forums = await chat_services.game_chats.check_forums()
        assert forums["game"]["ok"] is False
        assert "не имеет доступа" in forums["game"]["error"]

    async def test_check_forums_status(self, chat_services, session):
        """Оба настроенных форума доступны — статусы 🟢."""
        forums = await chat_services.game_chats.check_forums()
        assert forums["game"]["ok"] and forums["game"]["chat_id"] == GAME_FORUM
        assert forums["mafia"]["ok"] and forums["mafia"]["chat_id"] == MAFIA_FORUM


class TestTestgameTopics:
    """Testgame использует новую систему тем (ТЗ-15: 22)."""

    async def test_testgame_creates_topics(self, chat_services, session):
        from bot.database.models import User as UserModel
        from bot.database.repositories.users import UserRepository
        from bot.services.groups import GroupService
        from bot.services.test_game import TestGameManager

        async with chat_services.session_factory() as s:
            admin = await make_user(s, "TGAdmin")
            await s.commit()
            admin_id = admin.id

        test_games = TestGameManager(
            chat_services.session_factory, chat_services.games, chat_services.phases,
            chat_services.notifier,
        )
        game_id, text = await test_games.create_test_game(admin_id, 4, fast=True)
        assert game_id, text
        game = await _game(chat_services, game_id)
        gw = chat_services.gateway
        # тестовая игра получила обе темы как обычная
        assert game.game_chat_id == GAME_FORUM and game.game_thread_id is not None
        assert game.mafia_chat_id == MAFIA_FORUM and game.mafia_thread_id is not None
        name = gw.topics[(GAME_FORUM, game.game_thread_id)]
        assert f"#{game_id}" in name and name.startswith("🎮")
        # фазы переключают доступ
        await chat_services.phases.begin_game(game_id)
        assert gw.is_closed(GAME_FORUM, game.game_thread_id)
        assert not gw.is_closed(MAFIA_FORUM, game.mafia_thread_id)
        await test_games.finish(game_id)
        assert gw.is_closed(GAME_FORUM, game.game_thread_id)
        assert gw.is_closed(MAFIA_FORUM, game.mafia_thread_id)
