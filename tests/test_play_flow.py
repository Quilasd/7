"""Regression: flow «/start → 🎮 Играть → игровая экран → комната → вход».

Баг (живой Telegram): в раздел «🎮 Играть» зайти можно, но список игр/комнат
не отображается и начать/присоединиться нельзя. Причины:

1. ПУСТОЙ экран в группе был тупиком: «Открытых комнат в группе нет» +
   единственная кнопка «⬅️ Назад» — ни создания, ни входа по ID, ни подсказки
   про /createroom.
2. Кнопка «🏠 Создать комнату» из меню /start, нажатая В ГРУППЕ, запускала
   визард ГЛОБАЛЬНОЙ комнаты (group_id=None): комната создана, но экран
   «Играть» группы её НЕ показывает (изоляция ТЗ-11) — «игра не отображается».
3. Если у игрока есть только своя комната, список группы рисовался пустым
   заголовком без объяснения.

Фикс (только отображение/навигация, механика не тронута):
- пустой экран «Играть» показывает действия: в группе — кнопку «🏠 Создать
  комнату» (RoomCB group_room — тот же create_room_in_group с правом
  START_GAME, что и /createroom) и «➕ По ID»; в ЛС — «➕ По ID»;
- визард в группе не запускается, а перенаправляет на создание комнаты группы;
- в смешанном случае (своя комната есть, чужих нет) — понятная строка.
"""

from __future__ import annotations

import inspect

import bot.handlers.groups_admin as ga
import bot.handlers.rooms as rm
import bot.handlers.start as st
from bot.database.models import Game, GamePlayer, GameStatus, Room, RoomPlayer
from bot.database.repositories.rooms import RoomRepository
from bot.states import RoomCreationStates
from tests.conftest import make_user


class FakeCB:
    def __init__(self, telegram_id: int = 0) -> None:
        self.answers: list[str] = []
        self.message = None
        self.from_user = type("U", (), {"id": telegram_id})()

    async def answer(self, text: str | None = None, show_alert: bool = False, **kw) -> bool:
        self.answers.append(text or "")
        return True


class FakeState:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.data: dict = {}

    async def clear(self) -> None:
        self.states.append(None)

    async def set_state(self, state) -> None:
        self.states.append(state)

    async def update_data(self, data: dict) -> None:
        self.data.update(data)

    async def get_data(self) -> dict:
        return dict(self.data)


async def call(handler, **data):
    """Вызов хендлера ровно с параметрами его сигнатуры (DI по имени)."""
    sig = inspect.signature(handler)
    await handler(**{k: v for k, v in data.items() if k in sig.parameters})


class Spy:
    """Перехват edit_or_answer у всех задействованных модулей."""

    def __init__(self, monkeypatch) -> None:
        self.texts: list[str] = []
        self.kbs: list = []
        for mod in (st, rm, ga):
            monkeypatch.setattr(mod, "edit_or_answer", self._fake)

    async def _fake(self, cb, text, kb=None):
        self.texts.append(text)
        self.kbs.append(kb)

    def kb_texts(self, index: int = -1) -> list[list[str]]:
        kb = self.kbs[index]
        return [[b.text for b in row] for row in kb.inline_keyboard] if kb else []


class TestPlayScreen:
    """Экран «🎮 Играть» (cb_play): сценарии A–E и изоляция групп."""

    async def test_empty_group_screen_offers_actions(self, services, session, monkeypatch):
        """Регрессия бага: пустой экран группы — не тупик."""
        user = await make_user(session, "U")
        group = await services.groups.get_or_create(-1001000, "mafia")
        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=group)
        text = spy.texts[-1]
        assert "Открытых комнат в группе" in text and "нет" in text
        assert "/createroom" in text                       # подсказка о создании
        assert "ID" in text                                 # путь входа по ID
        rows = spy.kb_texts()
        assert rows[0] == ["🏠 Создать комнату"]           # кнопка создания комнаты группы
        assert rows[1] == ["➕ По ID", "⬅️ В меню"]

    async def test_empty_private_screen_offers_join_by_id(self, services, session, monkeypatch):
        user = await make_user(session, "U")
        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=None)
        assert "Создай свою" in spy.texts[-1]
        assert spy.kb_texts() == [["➕ По ID", "⬅️ В меню"]]

    async def test_group_list_isolated_from_other_groups_and_private(
        self, services, session, monkeypatch
    ):
        """§11 multi-server: «Играть» в группе A показывает только комнаты A;
        в группе B — только B; в ЛС — только глобальные."""
        host = await make_user(session, "Host")
        a = await services.groups.get_or_create(-1001100, "A")
        b = await services.groups.get_or_create(-1001200, "B")
        room_a = Room(creator_id=host.id, name="RoomA", max_players=10, min_players=4,
                      status="OPEN", group_id=a.id, settings={"roles": {"mafia": 1}})
        room_b = Room(creator_id=host.id, name="RoomB", max_players=10, min_players=4,
                      status="OPEN", group_id=b.id, settings={"roles": {"mafia": 1}})
        room_g = Room(creator_id=host.id, name="GlobalRoom", max_players=10, min_players=4,
                      status="OPEN", group_id=None, settings={"roles": {"mafia": 1}})
        session.add_all([room_a, room_b, room_g])
        await session.commit()

        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=host, group=a)
        assert "RoomA" in spy.texts[-1] and "RoomB" not in spy.texts[-1]
        assert "GlobalRoom" not in spy.texts[-1]

        await call(st.cb_play, callback=FakeCB(), session=session, db_user=host, group=b)
        assert "RoomB" in spy.texts[-1] and "RoomA" not in spy.texts[-1]

        await call(st.cb_play, callback=FakeCB(), session=session, db_user=host, group=None)
        assert "GlobalRoom" in spy.texts[-1]
        assert "RoomA" not in spy.texts[-1] and "RoomB" not in spy.texts[-1]

    async def test_my_room_button_when_group_list_empty(self, services, session, monkeypatch):
        """Смешанный случай: своя комната есть, комнат группы нет — экран
        объясняет это и даёт кнопку «Моя комната» (не пустой заголовок)."""
        user = await make_user(session, "U")
        group = await services.groups.get_or_create(-1001300, "mafia")
        room = Room(creator_id=user.id, name="Mine", max_players=10, min_players=4,
                    status="OPEN", group_id=None, settings={"roles": {"mafia": 1}})
        session.add(room)
        await session.flush()
        session.add(RoomPlayer(room_id=room.id, user_id=user.id, is_ready=False))
        await session.commit()

        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=group)
        assert "Открытых комнат в группе нет" in spy.texts[-1]
        assert any("🎭 Моя комната" in row[0] for row in spy.kb_texts())

    async def test_active_game_shows_status_button(self, services, session, monkeypatch):
        """Сценарий C: игрок в активной игре — экран игры, а не список."""
        user = await make_user(session, "U")
        game = Game(status=GameStatus.NIGHT.value, day_number=1,
                    max_players=4, settings={"roles": {"mafia": 1}})
        session.add(game)
        await session.flush()
        session.add(GamePlayer(game_id=game.id, user_id=user.id, role="citizen", is_alive=True))
        await session.commit()

        spy = Spy(monkeypatch)
        cb = FakeCB()
        await call(st.cb_play, callback=cb, session=session, db_user=user, group=None)
        assert "уже идёт игра" in spy.texts[-1]
        assert any("Состояние игры" in row[0] for row in spy.kb_texts())


class TestCreateRoomInGroup:
    """Кнопка «🏠 Создать комнату» с экрана «Играть» и защита визарда."""

    async def test_wizard_not_started_in_group(self, services, session, monkeypatch):
        """Регрессия бага: в группе визард глобальной комнаты не запускается —
        иначе созданная комната невидима для «Играть» этой группы."""
        user = await make_user(session, "U")
        group = await services.groups.get_or_create(-1001400, "mafia")
        spy = Spy(monkeypatch)
        state = FakeState()
        await call(rm.cb_create_start, callback=FakeCB(), state=state, group=group)
        assert RoomCreationStates.name not in state.states      # FSM не запущен
        assert "/createroom" in spy.texts[-1]                   # перенаправление
        assert spy.kb_texts()[0] == ["🏠 Создать комнату"]

    async def test_wizard_still_works_in_private(self, services, session, monkeypatch):
        user = await make_user(session, "U")
        Spy(monkeypatch)
        state = FakeState()
        await call(rm.cb_create_start, callback=FakeCB(), state=state, group=None)
        assert RoomCreationStates.name in state.states           # визард работает как раньше

    async def test_group_room_button_creates_room_with_permission(
        self, services, session, monkeypatch
    ):
        """Полный flow: кнопка создаёт комнату с правилами группы (как
        /createroom) и показывает её экран."""
        user = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-1001500, "mafia")
        monkeypatch.setattr(services.settings, "_owners", [user.telegram_id])

        spy = Spy(monkeypatch)
        cb = FakeCB(user.telegram_id)
        await call(ga.cb_create_group_room, callback=cb, session=session,
                   group=group, services=services, db_user=user)
        assert any("создана" in a for a in cb.answers)
        assert "МАФИЯ #" in spy.texts[-1]                       # экран комнаты
        async with services.session_factory() as s:
            rooms = await RoomRepository(s).for_group(group.id)
            assert len(rooms) == 1 and rooms[0].group_id == group.id
            assert rooms[0].creator_id == user.id
            assert [rp.user_id for rp in rooms[0].players] == [user.id]

    async def test_group_room_button_denied_without_permission(
        self, services, session, monkeypatch
    ):
        user = await make_user(session, "Plain")
        group = await services.groups.get_or_create(-1001600, "mafia")
        spy = Spy(monkeypatch)
        cb = FakeCB(user.telegram_id)
        await call(ga.cb_create_group_room, callback=cb, session=session,
                   group=group, services=services, db_user=user)
        assert any("Нет права" in a for a in cb.answers)
        async with services.session_factory() as s:
            assert await RoomRepository(s).for_group(group.id) == []   # комнаты нет

    async def test_group_room_button_requires_group(self, services, session, monkeypatch):
        user = await make_user(session, "U")
        Spy(monkeypatch)
        cb = FakeCB()
        await call(ga.cb_create_group_room, callback=cb, session=session,
                   group=None, services=services, db_user=user)
        assert any("группе" in a for a in cb.answers)


class TestFullPlayFlow:
    """/start → 🎮 Играть → экран комнаты → присоединение (сценарии B/D/E)."""

    async def test_play_to_room_view_to_join_button(self, services, session, monkeypatch):
        """Игрок B видит комнату группы на «Играть», открывает её —
        есть «➕ Присоединиться»; после входа — готовность."""
        host = await make_user(session, "Host")
        guest = await make_user(session, "Guest")
        monkeypatch.setattr(services.settings, "_owners", [host.telegram_id])
        group = await services.groups.get_or_create(-1001700, "mafia")

        # 1) хост создаёт комнату кнопкой с пустого экрана «Играть»
        await call(ga.cb_create_group_room, callback=FakeCB(host.telegram_id),
                   session=session, group=group, services=services, db_user=host)
        async with services.session_factory() as s:
            (room,) = await RoomRepository(s).for_group(group.id)
            room_id = room.id

        # 2) гость открывает «Играть» — комната в списке
        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=guest, group=group)
        assert f"#{room_id}" in spy.texts[-1]
        assert any(f"#{room_id}" in row[0] for row in spy.kb_texts())

        # 3) гость открывает комнату — кнопка «Присоединиться»
        await call(rm.cb_room_view, callback=FakeCB(),
                   callback_data=type("RD", (), {"room_id": room_id})(),
                   session=session, db_user=guest)
        rows = spy.kb_texts()
        assert any("Присоединиться" in row[0] for row in rows)

        # 4) гость входит (тот же сервис, что за кнопкой) — и попадает в комнату
        joined, result = await services.rooms.join(room_id, guest.id)
        assert joined is not None and "присоедин" in result.lower() or "в комнате" in result.lower()
        async with services.session_factory() as s:
            fresh = await RoomRepository(s).get(room_id)
            assert sorted(rp.user_id for rp in fresh.players) == sorted([host.id, guest.id])

        # 5) повторный вход отказывает без ошибок (сценарий D)
        again, msg = await services.rooms.join(room_id, guest.id)
        assert again is not None and "уже" in msg
