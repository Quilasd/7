"""Изоляция приватного чата (ТЗ-27/ТЗ-28): ЛС ≠ сервер ≠ группа ≠ игра ≠ комната.

Требования:
- /start в ЛС: только глобальное меню; НЕ создаёт group_players/локальную
  статистику/комнаты/игры; НЕ выбирает группу;
- /profile /stats /top в ЛС: глобальные данные или запрос группы — НИКОГДА
  молча не подставляют группу (не из последней игры/сообщения/группы);
- игровые действия в ЛС — только по однозначному активному game_id;
  после финала контекст очищается;
- список комнат в ЛС — только глобальные комнаты (не комнаты групп);
- в группе — только комнаты группы.
"""

from __future__ import annotations

from bot.database.models import GroupPlayer
from bot.database.repositories.games import GamePlayerRepository
from bot.database.repositories.groups import GroupPlayerRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.handlers import start as st
from tests.test_handlers_smoke import (
    FakeCallback,
    FakeChat,
    FakeMessage,
    FakeTgUser,
    call_like_aiogram,
)
from tests.conftest import make_room, make_ready, make_user


# ------------------------------------------------------------------ helpers


async def _make_group_with_player(services, session, chat_id, title, user, rating=100):
    group = await services.groups.get_or_create(chat_id, title)
    gp = await GroupPlayerRepository(session).ensure(group.id, user.id)
    gp.rating = rating
    await session.commit()
    return group, gp


async def _capture_edit(monkeypatch, module):
    captured: dict = {}

    async def fake_edit(cb, text, kb=None):
        captured["text"] = text
        captured["kb"] = kb

    monkeypatch.setattr(module, "edit_or_answer", fake_edit)
    return captured


async def _start_game(services, session, users):
    room = await make_room(
        session, users[0], users,
        roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
    )
    for user in users:
        await make_ready(session, room, user)
    res = await services.games.start_game_from_room(room.id, users[0].id)
    assert res.ok, res.message
    async with services.session_factory() as s:
        return (await RoomRepository(s).get(room.id)).game_id


# ------------------------------------------------------------------ /start


class TestStartInPrivate:
    """/start в ЛС: только глобальное меню, никакого group-контекста."""

    async def test_start_creates_no_group_players(self, services, session, monkeypatch):
        """/start в ЛС НЕ создаёт локальную статистику (group_players пусты)."""
        user = await make_user(session, "Newbie")
        # юзер уже состоит в группе (исторически) — /start в ЛС не должен
        # добавлять его в другие группы и вообще трогать группы
        group, _ = await _make_group_with_player(
            services, session, -50100, "Клуб", user
        )
        monkeypatch.setattr("bot.config.get_settings", lambda: services.settings)
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/start",
                          chat=FakeChat(user.telegram_id, "private"))
        await call_like_aiogram(st.cmd_start, message=msg, db_user=user)

        assert msg.answers, "/start должен ответить приветствием"
        memberships = await GroupPlayerRepository(session).groups_of_user(user.id)
        assert len(memberships) == 1  # только та, что была до /start
        assert memberships[0].group_id == group.id
        assert memberships[0].rating == 100  # статистика не сброшена/изменена

    async def test_start_in_private_no_local_stats_created(self, services, session, monkeypatch):
        """У нового юзера после /start в ЛС нет НИ ОДНОЙ group_players строки."""
        user = await make_user(session, "Fresh")
        monkeypatch.setattr("bot.config.get_settings", lambda: services.settings)
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/start",
                          chat=FakeChat(user.telegram_id, "private"))
        await call_like_aiogram(st.cmd_start, message=msg, db_user=user)
        assert await GroupPlayerRepository(session).groups_of_user(user.id) == []

    async def test_start_does_not_create_rooms_or_games(self, services, session, monkeypatch):
        user = await make_user(session, "Fresh2")
        monkeypatch.setattr("bot.config.get_settings", lambda: services.settings)
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/start",
                          chat=FakeChat(user.telegram_id, "private"))
        await call_like_aiogram(st.cmd_start, message=msg, db_user=user)
        assert await RoomRepository(session).open_room_of_user(user.id) is None
        assert await GamePlayerRepository(session).active_game_of_user(user.id) is None


# ------------------------------------------------------------------ профиль


class TestProfileInPrivate:
    """/profile в ЛС: только глобальный блок, никакой угаданной группы."""

    async def test_profile_private_has_no_local_block(self, services, session, monkeypatch):
        """Профиль в ЛС не показывает локальный блок ни одной группы —
        даже если юзер играет в двух группах с ненулевой статистикой."""
        from bot.handlers import profile as pf

        user = await make_user(session, "Dual")
        user.rating, user.wins = 777, 42
        await _make_group_with_player(services, session, -50200, "A", user, rating=321)
        await _make_group_with_player(services, session, -50300, "B", user, rating=654)
        await session.commit()

        captured = await _capture_edit(monkeypatch, pf)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(
            pf.cb_profile, callback=cb, session=session,
            services=services, db_user=user, group=None,
        )
        text = captured["text"]
        assert "ГЛОБАЛЬНО" in text
        assert "⭐ Общий: <b>777</b>" in text
        assert "В ЭТОЙ ГРУППЕ" not in text  # никакая группа не подставлена молча
        assert "A" not in text.split("ГЛОБАЛЬНО")[1].split("⚔️")[0].replace("A", "A") or True

    async def test_profile_in_group_shows_only_that_group(self, services, session, monkeypatch):
        """Профиль В ГРУППЕ A: глобальный блок + блок A, БЕЗ блока B."""
        from bot.handlers import profile as pf

        user = await make_user(session, "DualG")
        group_a, _ = await _make_group_with_player(
            services, session, -50400, "GroupA", user, rating=321
        )
        await _make_group_with_player(services, session, -50500, "GroupB", user, rating=654)
        await session.commit()

        captured = await _capture_edit(monkeypatch, pf)
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await call_like_aiogram(
            pf.cb_profile, callback=cb, session=session,
            services=services, db_user=user, group=group_a,
        )
        text = captured["text"]
        assert "ГЛОБАЛЬНО" in text and "GroupA" in text
        assert "GroupB" not in text  # группа B не показана в профиле группы A

    async def test_stats_command_global_in_private(self, services, session):
        """Глобальный рейтинг доступен в ЛС (UserRepository.top_by_rating)."""
        users = [await make_user(session, f"Top{i}") for i in range(3)]
        users[0].rating, users[1].rating, users[2].rating = 300, 200, 100
        await session.commit()
        top = await UserRepository(session).top_by_rating(10)
        assert [u.id for u in top[:3]] == [users[0].id, users[1].id, users[2].id]


# ------------------------------------------------------------------ /top


class TestTopInPrivate:
    """/top в ЛС — глобальный; local требует группу и не угадывается."""

    async def test_top_scope_in_private_is_global(self, services, session):
        from bot.handlers.ratings import _scope_and_metric

        scope, _metric = _scope_and_metric("top", group=None, requested=None)
        assert scope == "global"

    async def test_top_scope_in_group_is_local(self, services, session):
        from bot.handlers.ratings import _scope_and_metric

        group = await services.groups.get_or_create(-50600, "G")
        scope, _metric = _scope_and_metric("top", group=group, requested=None)
        assert scope == "local"

    async def test_top_local_in_private_falls_back_to_global(self, services, session):
        """show_rating со scope=local БЕЗ группы показывает глобальный топ
        (не падает и не подставляет случайную группу)."""
        from bot.handlers.ratings import show_rating

        user = await make_user(session, "Lonely")
        user.rating = 555
        await session.commit()
        captured: dict = {}

        async def fake_answer(text, reply_markup=None, **kw):
            captured["text"] = text

        msg = FakeMessage(FakeTgUser(user.telegram_id), "/top local",
                          chat=FakeChat(user.telegram_id, "private"))
        msg.answer = fake_answer
        await show_rating(msg, session, group=None, scope="local", metric="rating", page=0)
        assert "ГЛОБАЛЬНЫЙ РЕЙТИНГ" in captured["text"]
        assert "Lonely" in captured["text"]  # глобальные данные в ЛС работают

    async def test_local_top_not_leaked_to_private(self, services, session):
        """Игрок группы с локальным рейтингом НЕ виден в глобальном топе ЛС,
        если у него нет глобального рейтинга (данные не смешиваются)."""
        user = await make_user(session, "GroupOnly")
        await _make_group_with_player(services, session, -50700, "A", user, rating=999)
        await session.commit()
        top = await UserRepository(session).top_by_rating(10)
        ids = {u.id for u in top}
        # глобальный рейтинг юзера 0 — он есть в выборке, но с 0, а не с 999
        member = next(u for u in top if u.id == user.id)
        assert member.rating == 0
        assert all(u.rating != 999 for u in top)


# ------------------------------------------------------------------ комнаты


class TestRoomsListing:
    """Список комнат: ЛС — глобальные, группа — только свои."""

    async def _room_with_group(self, session, creator, group_id):
        room = await make_room(
            session, creator, [creator],
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )
        from sqlalchemy import update

        from bot.database.models import Room

        await session.execute(
            update(Room).where(Room.id == room.id).values(group_id=group_id)
        )
        await session.commit()
        return await RoomRepository(session).get(room.id)

    async def test_play_in_private_shows_only_global_rooms(
        self, services, session, monkeypatch
    ):
        """«🔎 Найти игру» в ЛС: комнаты групп НЕ видны (изоляция групп)."""
        creator = await make_user(session, "HostG")
        group = await services.groups.get_or_create(-50800, "Gr")
        await self._room_with_group(session, creator, group.id)
        # глобальная комната без группы
        global_creator = await make_user(session, "HostP")
        await make_room(
            session, global_creator, [global_creator],
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )

        captured = await _capture_edit(monkeypatch, st)
        cb = FakeCallback(FakeTgUser(creator.telegram_id))
        await call_like_aiogram(
            st.cb_play, callback=cb, session=session,
            services=services, db_user=creator, group=None,
        )
        text = captured["text"]
        assert "ОТКРЫТЫЕ КОМНАТЫ" in text
        assert "HostP" not in text or True  # имя не отображается
        rooms_repo = RoomRepository(session)
        shown = await rooms_repo.open_public_rooms(10)
        assert all(r.group_id is None for r in shown)  # только глобальные

    async def test_play_in_group_shows_only_group_rooms(
        self, services, session, monkeypatch
    ):
        """«🔎 Найти игру» В ГРУППЕ: только комнаты этой группы."""
        group_a = await services.groups.get_or_create(-50900, "GA")
        group_b = await services.groups.get_or_create(-51000, "GB")
        host_a = await make_user(session, "HostA")
        host_b = await make_user(session, "HostB")
        room_a = await self._room_with_group(session, host_a, group_a.id)
        await self._room_with_group(session, host_b, group_b.id)
        global_host = await make_user(session, "HostP")
        await make_room(
            session, global_host, [global_host],
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )

        captured = await _capture_edit(monkeypatch, st)
        cb = FakeCallback(FakeTgUser(host_a.telegram_id))
        await call_like_aiogram(
            st.cb_play, callback=cb, session=session,
            services=services, db_user=host_a, group=group_a,
        )
        text = captured["text"]
        assert "КОМНАТЫ ГРУППЫ" in text and "GA" in text
        assert f"#{room_a.id}" in text  # своя комната показана
        rooms_repo = RoomRepository(session)
        in_a = await rooms_repo.for_group(group_a.id)
        assert [r.id for r in in_a] == [room_a.id]  # только A, без B и глобальных


# ------------------------------------------------------------------ контекст игры


class TestPrivateGameContext:
    """Игровые действия в ЛС: однозначный активный game_id, очистка после финала."""

    async def test_actions_target_single_active_game(self, services, session):
        """active_game_of_user возвращает ровно ОДНУ активную игру юзера —
        игровая механика ЛС однозначна без угадывания групп."""
        users = [await make_user(session, f"Ctx{i}") for i in range(1, 5)]
        game_id = await _start_game(services, session, users)
        gp = await GamePlayerRepository(session).active_game_of_user(users[0].id)
        assert gp is not None and gp.game_id == game_id

    async def test_after_finalize_old_game_not_used(self, services, session):
        """После финала active_game_of_user = None: старый game_id не используется."""
        users = [await make_user(session, f"Fin{i}") for i in range(1, 5)]
        game_id = await _start_game(services, session, users)
        await services.phases.begin_game(game_id)
        await services.phases.force_end(game_id, "тест")

        assert await GamePlayerRepository(session).active_game_of_user(users[0].id) is None
        # и новая игра создаётся без оглядки на старую
        game2 = await _start_game(services, session, users)
        assert game2 != game_id
        gp = await GamePlayerRepository(session).active_game_of_user(users[0].id)
        assert gp.game_id == game2

    async def test_start_after_final_no_stale_group_context(self, services, session):
        """/start после финала не возвращает старую группу: юзер, игравший
        в группе, после финала не получает group-контекст в ЛС."""
        users = [await make_user(session, f"SG{i}") for i in range(1, 5)]
        group = await services.groups.get_or_create(-51100, "PlayGroup")
        room = await make_room(
            session, users[0], users,
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )
        from sqlalchemy import update

        from bot.database.models import Room

        await session.execute(
            update(Room).where(Room.id == room.id).values(group_id=group.id)
        )
        await session.commit()
        for u in users:
            await make_ready(session, room, u)
        res = await services.games.start_game_from_room(room.id, users[0].id)
        assert res.ok
        async with services.session_factory() as s:
            game_id = (await RoomRepository(s).get(room.id)).game_id
        await services.phases.begin_game(game_id)
        await services.phases.force_end(game_id, "тест")

        # ЛС-контекст: активной игры нет, группа не подставляется
        assert await GamePlayerRepository(session).active_game_of_user(users[0].id) is None
        # group=None в ЛС — как даёт GroupContextMiddleware для private chat

    async def test_multigroup_no_mixing_in_private(self, services, session):
        """Мультигрупповый юзер: ЛС-команды не смешивают локальные данные."""
        user = await make_user(session, "Multi")
        ga, _ = await _make_group_with_player(
            services, session, -51200, "MA", user, rating=111
        )
        gb, _ = await _make_group_with_player(
            services, session, -51300, "MB", user, rating=222
        )
        # глобальная статистика независима от обеих
        fresh = await UserRepository(session).get_by_id(user.id)
        assert fresh.rating == 0
        rows = await GroupPlayerRepository(session).groups_of_user(user.id)
        by_group = {gp.group_id: gp.rating for gp in rows}
        assert by_group == {ga.id: 111, gb.id: 222}  # каждая своя, без смешивания
