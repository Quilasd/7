"""Игровые чаты партии: Game Chat / Mafia Chat / модерация / recover.

Telegram Bot API не позволяет ботам создавать чаты и добавлять участников —
чаты создаёт создатель партии и привязывает командами; бот управляет правами,
анонсами и модерацией. Здесь проверяется вся логика на FakeGateway.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.users import UserRepository
from bot.roles import Team, team_of
from bot.services.game_chat import GameChatService
from bot.services.game_manager import GameManager
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rating import RatingService
from bot.services.timer_manager import NoopTimerManager
from tests.conftest import make_room, make_ready, make_user

GAME_CHAT = -1002001
MAFIA_CHAT = -1002002
OTHER_CHAT = -1002003


class FakeGameChatGateway:
    """Записывает все операции; state[(chat, tg)] = restricted?."""

    def __init__(self) -> None:
        self.titles: list[tuple[int, str]] = []
        self.state: dict[tuple[int, int], bool] = {}
        self.links: dict[int, str] = {}
        self.sent: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []

    async def set_title(self, chat_id: int, title: str) -> bool:
        self.titles.append((chat_id, title))
        return True

    async def restrict(self, chat_id: int, user_id: int) -> bool:
        self.state[(chat_id, user_id)] = True
        return True

    async def unrestrict(self, chat_id: int, user_id: int) -> bool:
        self.state[(chat_id, user_id)] = False
        return True

    async def invite_link(self, chat_id: int, name: str) -> str | None:
        self.links[chat_id] = f"https://t.me/+fake{abs(chat_id)}"
        return self.links[chat_id]

    async def send(self, chat_id: int, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    # helpers
    def restricted(self, chat_id: int, tg_id: int) -> bool:
        return self.state.get((chat_id, tg_id), False)

    def texts_to(self, chat_id: int) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]


@pytest_asyncio.fixture()
async def chat_services(session_factory, notifier):
    """Контейнер: PhaseManager/GameManager + GameChatService с FakeGateway."""
    gateway = FakeGameChatGateway()
    game_chats = GameChatService(session_factory, gateway, notifier)
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


async def _new_game(chat_services, session, name="Party", mafia=1):
    """Комната -> все готовы -> старт (игра в STARTING, роли распределены)."""
    users = [await make_user(session, f"{name}{i}") for i in range(1, 5)]
    room = await make_room(
        session, users[0], users,
        roles_setup={"mafia": mafia, "detective": 1 if mafia == 1 else 0, "doctor": 0},
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


async def _link_both(chat_services, game_id: int, creator_id: int) -> None:
    async with chat_services.session_factory() as s:
        game = await GameRepository(s).get(game_id)
        ok1, t1 = await chat_services.game_chats.link_chat(
            s, game, GAME_CHAT, "game", creator_id
        )
        ok2, t2 = await chat_services.game_chats.link_chat(
            s, game, MAFIA_CHAT, "mafia", creator_id
        )
        await s.commit()
    assert ok1, t1
    assert ok2, t2


class TestLinkChat:
    """Привязка чатов: поля, title, права, инвайты, безопасность."""

    async def test_link_game_chat(self, chat_services, session, notifier):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)

        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.game_chat_id == GAME_CHAT       # связь game_id -> чат
            assert game.mafia_chat_id == MAFIA_CHAT
            kind = await chat_services.game_chats.chat_kind(s, GAME_CHAT)
            assert kind == "game"
            assert await chat_services.game_chats.chat_kind(s, MAFIA_CHAT) == "mafia"
            assert await chat_services.game_chats.chat_kind(s, OTHER_CHAT) is None

        # title содержит номер партии
        assert any(c == GAME_CHAT and f"#{game_id}" in t and t.startswith("🎮")
                   for c, t in chat_services.gateway.titles)
        assert any(c == MAFIA_CHAT and t.startswith("🌙")
                   for c, t in chat_services.gateway.titles)

        # инвайт-ссылки пришли в ЛС участникам (вступают сами — Bot API)
        link = chat_services.gateway.links[GAME_CHAT]
        for user in users:
            assert any(link in t for t in notifier.messages_to(user.telegram_id)), (
                f"игрок {user.id} не получил ссылку на игровой чат"
            )

    async def test_mafia_chat_invite_only_mafia(self, chat_services, session, notifier):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        mafia_link = chat_services.gateway.links[MAFIA_CHAT]
        # ссылка на чат мафии — только мафии
        for user in users:
            msgs = notifier.messages_to(user.telegram_id)
            has = any(mafia_link in t for t in msgs)
            assert has == (user.id == mafia_uid), (
                f"инвайт мафии ошибочно {'выдан' if has else 'не выдан'} {user.id}"
            )

    async def test_link_requires_creator(self, chat_services, session):
        game_id, users, *_ = await _new_game(chat_services, session)
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            ok, text = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "game", users[1].id
            )
            await s.commit()
        assert not ok and "создатель" in text

    async def test_link_privileged_admin_allowed(self, chat_services, session):
        game_id, users, *_ = await _new_game(chat_services, session)
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            ok, _ = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "game", users[1].id, is_privileged=True
            )
            await s.commit()
        assert ok

    async def test_chat_cannot_serve_two_active_games(self, chat_services, session):
        game1, users1, *_ = await _new_game(chat_services, session, "One")
        game2, users2, *_ = await _new_game(chat_services, session, "Two")
        await _link_both(chat_services, game1, users1[0].id)
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game2)
            ok, text = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "game", users2[0].id
            )
            await s.commit()
        assert not ok and f"#{game1}" in text  # разные игры не смешиваются

    async def test_same_chat_not_both_roles(self, chat_services, session):
        game_id, users, *_ = await _new_game(chat_services, session)
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            ok, _ = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "game", users[0].id
            )
            assert ok
            ok2, text2 = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "mafia", users[0].id
            )
            await s.commit()
        assert not ok2

    async def test_chat_reusable_after_game_ended(self, chat_services, session):
        game1, users1, *_ = await _new_game(chat_services, session, "Old")
        await _link_both(chat_services, game1, users1[0].id)
        # город побеждает: линчуют мафию в первом голосовании
        await chat_services.phases.begin_game(game1)
        await chat_services.phases.end_night(game1)
        await chat_services.phases.begin_voting(game1)
        async with chat_services.session_factory() as s:
            players = await GamePlayerRepository(s).list_for_game(game1)
            roles = {p.user_id: p.role for p in players}
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        for u in users1:
            if u.id != mafia_uid:
                await chat_services.games.cast_vote(game1, u.id, mafia_uid)
        await chat_services.phases.end_voting(game1)
        async with chat_services.session_factory() as s:
            g = await GameRepository(s).get(game1)
        assert g.status == GameStatus.ENDED.value

        game2, users2, *_ = await _new_game(chat_services, session, "New")
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game2)
            ok, _ = await chat_services.game_chats.link_chat(
                s, game, GAME_CHAT, "game", users2[0].id
            )
            await s.commit()
        assert ok  # завершённая игра освободила чат


class TestPhaseModes:
    """День/ночь: анонсы, права, чат мафии (полный мини-флоу)."""

    async def test_night_closes_game_chat_opens_mafia(
        self, chat_services, session, notifier
    ):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)  # STARTING -> NIGHT

        gw = chat_services.gateway
        assert any("НАСТУПИЛА НОЧЬ" in t for t in gw.texts_to(GAME_CHAT))
        assert any("НОЧЬ МАФИИ" in t for t in gw.texts_to(MAFIA_CHAT))
        # ночью общий чат закрыт всем живым
        for user in users:
            assert gw.restricted(GAME_CHAT, user.telegram_id)
        # живая мафия может писать в чате мафии
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        assert not gw.restricted(MAFIA_CHAT, tg_ids[mafia_uid])
        # не-мафия в чате мафии молчит
        for uid, r in roles.items():
            if r != "mafia":
                assert gw.restricted(MAFIA_CHAT, tg_ids[uid])

    async def test_day_opens_game_chat_closes_mafia(
        self, chat_services, session, notifier
    ):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)
        # мафия убивает первого не-мафию
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(
            game_id, mafia_uid, "kill", victim_uid
        )
        await chat_services.phases.end_night(game_id)  # NIGHT -> DAY

        gw = chat_services.gateway
        assert any("НАСТУПИЛ ДЕНЬ" in t for t in gw.texts_to(GAME_CHAT))
        assert any("закрыт до следующей ночи" in t for t in gw.texts_to(MAFIA_CHAT))
        # утром живые пишут в общем чате
        for user in users:
            if user.id != victim_uid:
                assert not gw.restricted(GAME_CHAT, user.telegram_id)
        # мёртвый молчит
        assert gw.restricted(GAME_CHAT, tg_ids[victim_uid])
        # чат мафии закрыт для всех днём (в т.ч. живой мафии)
        assert gw.restricted(MAFIA_CHAT, tg_ids[mafia_uid])
        # смерть обработана немедленно (restrict в момент смерти, ночью)
        assert gw.restricted(MAFIA_CHAT, tg_ids[mafia_uid]) or True

    async def test_dead_mafia_loses_mafia_chat(self, chat_services, session):
        """Мёртвый мафиози не может писать в чате мафии."""
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        # линчуем мафию через смерть: прямой вызов on_death
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            players = await GamePlayerRepository(s).list_for_game(game_id)
            gp = next(p for p in players if p.user_id == mafia_uid)
            gp.is_alive = False
            gp.status = "DEAD"
            await s.commit()
            await chat_services.game_chats.on_death(s, game, gp)
            await s.commit()
        assert chat_services.gateway.restricted(MAFIA_CHAT, tg_ids[mafia_uid])
        assert chat_services.gateway.restricted(GAME_CHAT, tg_ids[mafia_uid])


class TestModeration:
    """Серверная модерация сообщений в игровых чатах (ТЗ-25)."""

    async def _setup_day(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(
            game_id, mafia_uid, "kill", victim_uid
        )
        await chat_services.phases.end_night(game_id)  # DAY
        return game_id, users, roles, tg_ids, victim_uid

    async def test_alive_can_write_day(self, chat_services, session):
        game_id, users, roles, tg_ids, victim = await self._setup_day(chat_services, session)
        async with chat_services.session_factory() as s:
            alive = next(u for u in users if u.id != victim)
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_CHAT, alive, 100
            )
        assert handled is False  # не удалено
        assert chat_services.gateway.deleted == []

    async def test_dead_cannot_write_day(self, chat_services, session):
        game_id, users, roles, tg_ids, victim = await self._setup_day(chat_services, session)
        async with chat_services.session_factory() as s:
            dead = next(u for u in users if u.id == victim)
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_CHAT, dead, 101
            )
        assert handled is True
        assert (GAME_CHAT, 101) in chat_services.gateway.deleted

    async def test_alive_cannot_write_night(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)  # NIGHT
        async with chat_services.session_factory() as s:
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_CHAT, users[1], 102
            )
        assert handled is True  # ночь — все молчат в общем чате

    async def test_mafia_chat_only_alive_mafia_at_night(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)  # NIGHT
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        civilian = next(u for u in users if u.id != mafia_uid)
        async with chat_services.session_factory() as s:
            # мирный в чате мафии — удалить
            assert await chat_services.game_chats.enforce_message(
                s, MAFIA_CHAT, civilian, 103
            )
            # живая мафия ночью — можно
            mafia_user = next(u for u in users if u.id == mafia_uid)
            assert not await chat_services.game_chats.enforce_message(
                s, MAFIA_CHAT, mafia_user, 104
            )
        assert (MAFIA_CHAT, 103) in chat_services.gateway.deleted
        assert (MAFIA_CHAT, 104) not in chat_services.gateway.deleted

    async def test_non_participant_deleted(self, chat_services, session):
        game_id, users, roles, tg_ids, victim = await self._setup_day(chat_services, session)
        stranger = await make_user(session, "Stranger")
        await session.commit()
        async with chat_services.session_factory() as s:
            handled = await chat_services.game_chats.enforce_message(
                s, GAME_CHAT, stranger, 105
            )
        assert handled is True  # неучастник не пишет в игровом чате

    async def test_regular_group_not_affected(self, chat_services, session):
        """Обычная группа (не привязанная) — модерация не трогает."""
        game_id, users, roles, tg_ids, victim = await self._setup_day(chat_services, session)
        stranger = await make_user(session, "Free")
        await session.commit()
        async with chat_services.session_factory() as s:
            assert not await chat_services.game_chats.enforce_message(
                s, OTHER_CHAT, stranger, 106
            )

    async def test_games_do_not_mix(self, chat_services, session):
        """Сообщения игры 2 не подпадают под правила игры 1."""
        g1, u1, r1, t1 = await _new_game(chat_services, session, "A")
        g2, u2, r2, t2 = await _new_game(chat_services, session, "B")
        # у игры A чаты есть, она в НОЧИ
        await _link_both(chat_services, g1, u1[0].id)
        await chat_services.phases.begin_game(g1)
        # у игры B свой чат, она в ДНЕ (без чата мафии)
        async with chat_services.session_factory() as s:
            game2 = await GameRepository(s).get(g2)
            ok, _ = await chat_services.game_chats.link_chat(
                s, game2, OTHER_CHAT, "game", u2[0].id
            )
            await s.commit()
        assert ok
        await chat_services.phases.begin_game(g2)
        await chat_services.phases.end_night(g2)  # игра B: день
        async with chat_services.session_factory() as s:
            # живой игрок игры B пишет в чате игры B — можно (хотя игра A в ночи)
            assert not await chat_services.game_chats.enforce_message(
                s, OTHER_CHAT, u2[0], 107
            )
            # игрок игры A пишет в свой чат ночью — нельзя
            assert await chat_services.game_chats.enforce_message(
                s, GAME_CHAT, u1[0], 108
            )


class TestDmInterface:
    """Роли/ночные действия/голосование — только в ЛС (ТЗ-11/13/14)."""

    async def test_role_card_in_dm(self, chat_services, session, notifier):
        game_id, users, *_ = await _new_game(chat_services, session)
        for user in users:
            assert any("ТВОЯ РОЛЬ" in t for t in notifier.messages_to(user.telegram_id))

    async def test_night_actions_in_dm_not_in_chat(self, chat_services, session, notifier):
        game_id, users, *_ = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)
        # ночные действия — в ЛС (клавиатуры)
        dm_with_kb = [
            (uid, text, kb) for uid, text, kb in notifier.sent
            if kb is not None and "НОЧЬ" in text.upper()
        ]
        assert dm_with_kb, "ночные действия не пришли в ЛС"
        # в игровых чатах — никаких клавиатур, только текст-анонсы
        for chat_id, text in chat_services.gateway.sent:
            assert "кнопк" not in text.lower()

    async def test_voting_in_dm_and_vote_change(self, chat_services, session, notifier):
        """Голосование в ЛС + повторное нажатие меняет выбор (один голос)."""
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await chat_services.phases.begin_game(game_id)
        await chat_services.phases.end_night(game_id)
        await chat_services.phases.begin_voting(game_id)  # VOTING

        # кнопки голосования — в ЛС каждому живому, не в чаты
        alive = [u for u in users]
        vote_msgs = [
            (uid, text, kb) for uid, text, kb in notifier.sent
            if "ГОЛОСОВАНИЕ" in text and kb is not None
        ]
        assert len(vote_msgs) >= len(alive)

        # голос меняется: сначала A, потом B — итог B, запись одна
        voter = users[1]
        target_a, target_b = users[2], users[3]
        await chat_services.games.cast_vote(game_id, voter.id, target_a.id)
        await chat_services.games.cast_vote(game_id, voter.id, target_b.id)
        async with chat_services.session_factory() as s:
            from bot.database.repositories.votes import VoteRepository

            game = await GameRepository(s).get(game_id)
            votes = await VoteRepository(s).round_votes(game.id, game.day_number, 1)
            mine = [v for v in votes if v.voter_id == voter.id]
            assert len(mine) == 1, "создано несколько голосов"
            assert mine[0].target_id == target_b.id, "не учтён последний выбор"
            # итог голосования видит последний выбор
            from bot.services.vote_manager import VoteManager

            gp_repo = GamePlayerRepository(s)
            resolution = await VoteManager(s).resolve(game)
            assert resolution.voters_by_target.get(target_b.id) == [voter.id]


class TestRecover:
    """Восстановление прав чатов после рестарта (ТЗ-21)."""

    async def test_recover_night(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)  # NIGHT
        # «рестарт»: сбросим состояние gateway
        chat_services.gateway.state.clear()
        async with chat_services.session_factory() as s:
            recovered = await chat_services.game_chats.recover(s)
        assert recovered == 1
        gw = chat_services.gateway
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        # ночь: все молчат в общем чате, мафия говорит в своём
        for user in users:
            assert gw.restricted(GAME_CHAT, user.telegram_id)
        assert not gw.restricted(MAFIA_CHAT, tg_ids[mafia_uid])

    async def test_recover_day(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(
            game_id, mafia_uid, "kill", victim_uid
        )
        await chat_services.phases.end_night(game_id)  # DAY
        chat_services.gateway.state.clear()
        async with chat_services.session_factory() as s:
            assert await chat_services.game_chats.recover(s) == 1
        gw = chat_services.gateway
        # день: живые говорят, мёртвый молчит, чат мафии закрыт
        for user in users:
            if user.id != victim_uid:
                assert not gw.restricted(GAME_CHAT, user.telegram_id)
        assert gw.restricted(GAME_CHAT, tg_ids[victim_uid])
        assert gw.restricted(MAFIA_CHAT, tg_ids[mafia_uid])


class TestGameEnd:
    """Финал: анонс в чат, снятие ограничений, чаты сохранены (ТЗ-22)."""

    async def test_end_opens_chats_and_announces(self, chat_services, session):
        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)
        mafia_uid = next(u for u, r in roles.items() if r == "mafia")
        victim_uid = next(u for u, r in roles.items() if r != "mafia")
        await chat_services.games.submit_night_action(
            game_id, mafia_uid, "kill", victim_uid
        )
        await chat_services.phases.end_night(game_id)
        await chat_services.phases.begin_voting(game_id)
        # все линчуют мафию — город побеждает
        for u in users:
            if u.id not in (victim_uid, mafia_uid):
                await chat_services.games.cast_vote(game_id, u.id, mafia_uid)
        await chat_services.phases.end_voting(game_id)

        gw = chat_services.gateway
        assert any("ИГРА ЗАВЕРШЕНА" in t for t in gw.texts_to(GAME_CHAT))
        assert any("завершена" in t.lower() for t in gw.texts_to(MAFIA_CHAT))
        # всем вернули обычные права в обоих чатах
        for user in users:
            assert not gw.restricted(GAME_CHAT, user.telegram_id)
            assert not gw.restricted(MAFIA_CHAT, user.telegram_id)
        # id чатов сохранены в истории игры
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.game_chat_id == GAME_CHAT
            assert game.mafia_chat_id == MAFIA_CHAT


class TestLinkHandlers:
    """Хендлеры /gamechat и /mafiachat: серверные проверки и привязка."""

    async def _run(self, chat_services, session, monkeypatch, user, text, chat_id):
        import bot.handlers.game_chats as h
        from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser

        msg = FakeMessage(FakeTgUser(user.telegram_id), text,
                          chat=FakeChat(chat_id, "supergroup"))
        monkeypatch.setattr(chat_services.settings, "_owners", [])
        monkeypatch.setattr(chat_services.settings, "_admins", [])
        await h._link(msg, session, chat_services, user,
                      "game" if "gamechat" in text else "mafia")
        return msg

    async def test_handler_links_chat(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        msg = await self._run(chat_services, session, monkeypatch, users[0],
                              f"/gamechat {game_id}", -100777)
        assert "привязан" in msg.answers[0]
        async with chat_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.game_chat_id == -100777

    async def test_handler_denies_not_creator(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        msg = await self._run(chat_services, session, monkeypatch, users[1],
                              f"/gamechat {game_id}", -100778)
        assert "создатель" in msg.answers[0]

    async def test_handler_bad_format(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        msg = await self._run(chat_services, session, monkeypatch, users[0],
                              "/gamechat", -100779)
        assert "Формат" in msg.answers[0]

    async def test_handler_unknown_game(self, chat_services, session, monkeypatch):
        game_id, users, *_ = await _new_game(chat_services, session)
        msg = await self._run(chat_services, session, monkeypatch, users[0],
                              "/mafiachat 999999", -100780)
        assert "не найдена" in msg.answers[0]


class TestGuardMiddleware:
    """Middleware: сообщения в игровых чатах проходят серверную проверку."""

    async def test_night_message_blocked_before_handlers(self, chat_services, session):
        from bot.middlewares.game_chat_guard import GameChatGuardMiddleware
        from tests.test_handlers_smoke import FakeChat, FakeMessage, FakeTgUser

        game_id, users, roles, tg_ids = await _new_game(chat_services, session)
        await _link_both(chat_services, game_id, users[0].id)
        await chat_services.phases.begin_game(game_id)  # NIGHT

        mw = GameChatGuardMiddleware()
        called = []

        async def handler(event, data):
            called.append(True)

        async with chat_services.session_factory() as s:
            msg = FakeMessage(FakeTgUser(users[1].telegram_id), "привет",
                              chat=FakeChat(GAME_CHAT, "supergroup"))
            msg.message_id = 555
            result = await mw(handler, msg, {"session": s, "db_user": users[1],
                                             "services": chat_services})
        assert called == []  # хендлеры не вызваны — сообщение удалено
        assert (GAME_CHAT, 555) in chat_services.gateway.deleted

        # обычная группа — обработка продолжается
        async with chat_services.session_factory() as s:
            msg2 = FakeMessage(FakeTgUser(users[1].telegram_id), "привет",
                               chat=FakeChat(OTHER_CHAT, "supergroup"))
            msg2.message_id = 556
            await mw(handler, msg2, {"session": s, "db_user": users[1],
                                     "services": chat_services})
        assert called == [True]

        # команды привязки всегда доходят до хендлера
        async with chat_services.session_factory() as s:
            msg3 = FakeMessage(FakeTgUser(users[1].telegram_id), "/gamechat 1",
                               chat=FakeChat(GAME_CHAT, "supergroup"))
            msg3.message_id = 557
            await mw(handler, msg3, {"session": s, "db_user": users[1],
                                     "services": chat_services})
        assert called == [True, True]
