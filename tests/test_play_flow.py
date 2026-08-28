"""Regression: flow «/start → 🎮 Играть → игровая экран → комната → вход».

Раунд 1 (тупик и «невидимые» комнаты): пустой экран «Играть» в группе был
тупиком, а «🏠 Создать комнату» из группы создавала ГЛОБАЛЬНУЮ комнату,
невидимую для «Играть» группы. Фикс дал кнопки действий.

Раунд 2 (регрессия фикса, этот файл обновлён): кнопка «🏠 Создать комнату»
из группы создавала комнату НАПРЯМУЮ через create_room_in_group — БЕЗ визарда
(мин/макс игроков и состав ролей выбрать нельзя). Фикс: из группы запускается
ТОТ ЖЕ полный визард (имя → максимум → минимум → приватность → роли →
settings_done), только с group_id текущей группы в FSM-данных и правом
START_GAME; «урезанный» прямой callback (RoomCB group_room) удалён.

Проверяемые инварианты:
- визард из группы: все 5 шагов на месте, выбранные значения сохраняются,
  комната получает group_id группы;
- визард из ЛС: работает как раньше, group_id=None;
- /createroom в группе: работает как раньше (быстрая комната с правилами
  группы);
- изоляция: комната группы A не видна в B и в ЛС;
- права: без START_GAME визард в группе не стартует.
"""

from __future__ import annotations

import inspect

import bot.handlers.rooms as rm
import bot.handlers.start as st
from bot.database.models import Game, GamePlayer, GameStatus, Room, RoomPlayer
from bot.database.repositories.rooms import RoomRepository
from bot.states import RoomCreationStates
from bot.utils.callbacks import MenuCB, RoomCB, RoomCreateCB
from tests.conftest import make_user


class FakeCB:
    def __init__(self, telegram_id: int = 0) -> None:
        self.answers: list[str] = []
        self.kbs: list = []
        self.message = None
        self.from_user = type("U", (), {"id": telegram_id})()

    async def answer(self, text: str | None = None, show_alert: bool = False,
                     reply_markup=None, **kw) -> bool:
        self.answers.append(text or "")
        self.kbs.append(reply_markup)
        return True

    def kb_texts(self) -> list[list[str]]:
        kb = self.kbs[-1] if self.kbs else None
        return [[b.text for b in row] for row in kb.inline_keyboard] if kb else []


class FakeMsg:
    def __init__(self, text: str, telegram_id: int = 0) -> None:
        self.text = text
        self.answers: list[str] = []
        self.kbs: list = []
        self.reply_to_message = None
        self.from_user = type("U", (), {"id": telegram_id})()

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:
        self.answers.append(text)
        self.kbs.append(reply_markup)

    def kb_texts(self) -> list[list[str]]:
        kb = self.kbs[-1] if self.kbs else None
        return [[b.text for b in row] for row in kb.inline_keyboard] if kb else []


class FakeState:
    """FSM-контекст: data сохраняется, set_state/clear пишутся в journal."""

    def __init__(self) -> None:
        self.states: list = []
        self.data: dict = {}

    async def clear(self) -> None:
        self.states.append(None)
        self.data = {}

    async def set_state(self, state) -> None:
        self.states.append(state)

    async def update_data(self, data: dict | None = None, **kwargs) -> None:
        self.data.update(data or {})
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)


async def call(handler, **data):
    """Вызов хендлера ровно с параметрами его сигнатуры (DI по имени)."""
    sig = inspect.signature(handler)
    await handler(**{k: v for k, v in data.items() if k in sig.parameters})


class Spy:
    """Перехват edit_or_answer у задействованных модулей."""

    def __init__(self, monkeypatch) -> None:
        self.texts: list[str] = []
        self.kbs: list = []
        for mod in (st, rm):
            monkeypatch.setattr(mod, "edit_or_answer", self._fake)

    async def _fake(self, cb, text, kb=None):
        self.texts.append(text)
        self.kbs.append(kb)

    def kb_texts(self, index: int = -1) -> list[list[str]]:
        kb = self.kbs[index]
        return [[b.text for b in row] for row in kb.inline_keyboard] if kb else []

    def kb_datas(self, index: int = -1) -> list[list[str]]:
        kb = self.kbs[index]
        return [[b.callback_data for b in row] for row in kb.inline_keyboard] if kb else []


async def _run_wizard(services, db_user, *, group, name="Тест комната",
                      maxp="8", minp="5", role_bumps=()):
    """Полный визард до создания комнаты; возвращает (state, callback)."""
    state = FakeState()
    cb = FakeCB(db_user.telegram_id)
    await call(rm.cb_create_start, callback=cb, state=state, session=None,
               services=services, db_user=db_user, group=group)
    await call(rm.wizard_name, message=FakeMsg(name), state=state)
    await call(rm.wizard_maxp, callback=FakeCB(db_user.telegram_id),
               callback_data=RoomCreateCB(action="maxp", value=maxp), state=state)
    await call(rm.wizard_minp, callback=FakeCB(db_user.telegram_id),
               callback_data=RoomCreateCB(action="minp", value=minp), state=state)
    await call(rm.wizard_privacy, callback=FakeCB(db_user.telegram_id),
               callback_data=RoomCreateCB(action="privacy", value="public"),
               state=state, services=services)
    for role_id, delta in role_bumps:
        for _ in range(delta):
            await call(rm.cb_role_inc, callback=FakeCB(db_user.telegram_id),
                       callback_data=RoomCB(action="roleinc", room_id=0, value=role_id),
                       services=services, state=state, db_user=db_user)
    await call(rm.cb_settings_done, callback=FakeCB(db_user.telegram_id),
               callback_data=RoomCB(action="settings_done", room_id=0),
               state=state, services=services, db_user=db_user)
    return state, cb


class TestPlayScreen:
    """Экран «🎮 Играть» (cb_play): сценарии A–E и изоляция групп."""

    async def test_empty_group_screen_offers_actions(self, services, session, monkeypatch):
        """Регрессия тупика: пустой экран группы даёт действия; кнопка
        создания ведёт в ПОЛНЫЙ визард (тот же callback, что в меню)."""
        user = await make_user(session, "U")
        group = await services.groups.get_or_create(-1001000, "mafia")
        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=group)
        text = spy.texts[-1]
        assert "Открытых комнат в группе" in text and "нет" in text
        assert "/createroom" in text
        assert "ID" in text
        rows = spy.kb_texts()
        assert rows[0] == ["🏠 Создать комнату"]
        assert rows[1] == ["➕ По ID", "⬅️ В меню"]
        # кнопка создания = вход существующего визарда (MenuCB create_room)
        assert spy.kb_datas()[0] == [MenuCB(action="create_room").pack()]

    async def test_empty_private_screen_offers_join_by_id(self, services, session, monkeypatch):
        user = await make_user(session, "U")
        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=None)
        assert "Создай свою" in spy.texts[-1]
        assert spy.kb_texts() == [["➕ По ID", "⬅️ В меню"]]

    async def test_group_list_isolated_from_other_groups_and_private(
        self, services, session, monkeypatch
    ):
        """§11 multi-server: «Играть» в группе A показывает только комнаты A."""
        host = await make_user(session, "Host")
        a = await services.groups.get_or_create(-1001100, "A")
        b = await services.groups.get_or_create(-1001200, "B")
        session.add_all([
            Room(creator_id=host.id, name="RoomA", max_players=10, min_players=4,
                 status="OPEN", group_id=a.id, settings={"roles": {"mafia": 1}}),
            Room(creator_id=host.id, name="RoomB", max_players=10, min_players=4,
                 status="OPEN", group_id=b.id, settings={"roles": {"mafia": 1}}),
            Room(creator_id=host.id, name="GlobalRoom", max_players=10, min_players=4,
                 status="OPEN", group_id=None, settings={"roles": {"mafia": 1}}),
        ])
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
        user = await make_user(session, "U")
        game = Game(status=GameStatus.NIGHT.value, day_number=1,
                    max_players=4, settings={"roles": {"mafia": 1}})
        session.add(game)
        await session.flush()
        session.add(GamePlayer(game_id=game.id, user_id=user.id, role="citizen", is_alive=True))
        await session.commit()

        spy = Spy(monkeypatch)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=user, group=None)
        assert "уже идёт игра" in spy.texts[-1]
        assert any("Состояние игры" in row[0] for row in spy.kb_texts())


class TestGroupWizard:
    """Полный визард из группы: те же шаги, но group_id = текущая группа."""

    async def test_wizard_in_group_starts_with_group_id(self, services, session, monkeypatch):
        """Вход в визард из группы: FSM запущен, group_id сохранён,
        приветствие — про комнату группы."""
        user = await make_user(session, "Boss")
        monkeypatch.setattr(services.settings, "_owners", [user.telegram_id])
        group = await services.groups.get_or_create(-1001400, "mafia")
        spy = Spy(monkeypatch)
        state = FakeState()
        await call(rm.cb_create_start, callback=FakeCB(user.telegram_id), state=state,
                   session=session, services=services, db_user=user, group=group)
        assert RoomCreationStates.name in state.states
        assert state.data.get("group_id") == group.id
        assert "СОЗДАНИЕ КОМНАТЫ ГРУППЫ" in spy.texts[-1]

    async def test_wizard_in_group_denied_without_permission(self, services, session, monkeypatch):
        """Право START_GAME обязательно (как у /createroom)."""
        user = await make_user(session, "Plain")
        group = await services.groups.get_or_create(-1001450, "mafia")
        Spy(monkeypatch)
        state = FakeState()
        cb = FakeCB(user.telegram_id)
        await call(rm.cb_create_start, callback=cb, state=state, session=session,
                   services=services, db_user=user, group=group)
        assert any("START_GAME" in a for a in cb.answers)
        assert RoomCreationStates.name not in state.states
        assert not state.data

    async def test_wizard_shows_min_max_and_roles_steps(self, services, session, monkeypatch):
        """Все шаги визарда на месте: выбор максимума, минимума, набор ролей."""
        user = await make_user(session, "Boss")
        monkeypatch.setattr(services.settings, "_owners", [user.telegram_id])
        group = await services.groups.get_or_create(-1001500, "mafia")
        spy = Spy(monkeypatch)
        state = FakeState()
        await call(rm.cb_create_start, callback=FakeCB(user.telegram_id), state=state,
                   session=session, services=services, db_user=user, group=group)
        name_msg = FakeMsg("Наша партия")
        await call(rm.wizard_name, message=name_msg, state=state)
        # шаг 2: максимум — кнопки-выбор значений (6/8/10/12/16/20)
        max_rows = name_msg.kb_texts()
        assert [b for row in max_rows for b in row] == \
            ["👥 6", "👥 8", "👥 10", "👥 12", "👥 16", "👥 20"]
        await call(rm.wizard_maxp, callback=FakeCB(user.telegram_id),
                   callback_data=RoomCreateCB(action="maxp", value="8"), state=state)
        # шаг 3: минимум (с учётом максимума)
        assert "Минимум для старта" in spy.texts[-1]
        await call(rm.wizard_minp, callback=FakeCB(user.telegram_id),
                   callback_data=RoomCreateCB(action="minp", value="5"), state=state)
        # шаг 4: приватность
        assert "Тип комнаты" in spy.texts[-1]
        # шаг 5: роли — ➖/➕ по каждой роли и «Создать комнату»
        # (шаг ролей рендерится через event.answer — не aiogram-событие в тесте)
        privacy_cb = FakeCB(user.telegram_id)
        await call(rm.wizard_privacy, callback=privacy_cb,
                   callback_data=RoomCreateCB(action="privacy", value="public"),
                   state=state, services=services)
        assert any("НАБОР РОЛЕЙ" in a for a in privacy_cb.answers)
        flat = [b for row in privacy_cb.kb_texts() for b in row]
        assert any("➕" == b for b in flat) and any("➖" == b for b in flat)
        assert any("Создать комнату" in b for b in flat)
        assert RoomCreationStates.roles in state.states

    async def test_wizard_from_group_saves_all_selected_values(self, services, session, monkeypatch):
        """Выбранные min/max/роли реально сохраняются; group_id правильный."""
        user = await make_user(session, "Boss")
        monkeypatch.setattr(services.settings, "_owners", [user.telegram_id])
        group = await services.groups.get_or_create(-1001600, "mafia")
        Spy(monkeypatch)
        await _run_wizard(services, user, group=group, name="Групповая",
                          maxp="8", minp="5", role_bumps=(("mafia", 1),))
        async with services.session_factory() as s:
            rooms = await RoomRepository(s).for_group(group.id)
            assert len(rooms) == 1
            room = rooms[0]
            assert room.group_id == group.id                 # скоуп группы
            assert room.max_players == 8 and room.min_players == 5
            assert room.creator_id == user.id and not room.is_private
            roles = room.settings["roles"]
            assert roles["mafia"] == 2                       # 1 (дефолт) + 1 (кнопка ➕)
            assert roles["detective"] == 1 and roles["doctor"] == 1

    async def test_wizard_from_private_creates_global_room(self, services, session, monkeypatch):
        """ЛС-creation не изменилась: те же шаги, group_id=None."""
        user = await make_user(session, "Solo")
        Spy(monkeypatch)
        state = FakeState()
        await call(rm.cb_create_start, callback=FakeCB(user.telegram_id), state=state,
                   session=session, services=services, db_user=user, group=None)
        assert RoomCreationStates.name in state.states
        assert "group_id" not in state.data
        await _run_wizard(services, user, group=None, name="Личная",
                          maxp="10", minp="4")
        async with services.session_factory() as s:
            (room,) = await RoomRepository(s).open_public_rooms(10)
            assert room.group_id is None
            assert room.max_players == 10 and room.min_players == 4

    async def test_createroom_command_path_unchanged(self, services, session):
        """B: /createroom в группе работает как раньше — быстрая комната
        с правилами группы (create_room_in_group)."""
        user = await make_user(session, "Admin")
        group = await services.groups.get_or_create(-1001700, "mafia")
        room, result = await services.groups.create_room_in_group(group.id, user.id)
        assert room is not None and room.group_id == group.id
        assert room.creator_id == user.id
        assert "создана" in result.lower()


class TestFullPlayFlow:
    """/start → 🎮 Играть → визард группы → комната → другая группа/игроки."""

    async def test_full_flow_group_wizard_to_join(self, services, session, monkeypatch):
        """Полный flow: визард из группы A (min/max/роли) → группа B комнату
        НЕ видит → игрок группы A видит её на «Играть» и в комнате — вход."""
        host = await make_user(session, "Host")
        guest = await make_user(session, "Guest")
        monkeypatch.setattr(services.settings, "_owners", [host.telegram_id])
        a = await services.groups.get_or_create(-1001800, "A")
        b = await services.groups.get_or_create(-1001900, "B")

        spy = Spy(monkeypatch)
        await _run_wizard(services, host, group=a, name="Партия A",
                          maxp="8", minp="5", role_bumps=(("mafia", 1),))
        async with services.session_factory() as s:
            (room,) = await RoomRepository(s).for_group(a.id)
            room_id = room.id
            assert await RoomRepository(s).for_group(b.id) == []    # F: изоляция

        # B не видит комнату A на «Играть», A — видит (§11/F)
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=host, group=b)
        assert f"#{room_id}" not in spy.texts[-1]
        await call(st.cb_play, callback=FakeCB(), session=session, db_user=guest, group=a)
        assert f"#{room_id}" in spy.texts[-1]

        # E: игрок группы A открывает комнату — видит её настройки (лимиты)
        await call(rm.cb_room_view, callback=FakeCB(guest.telegram_id),
                   callback_data=RoomCB(action="view", room_id=room_id),
                   session=session, db_user=guest)
        view = spy.texts[-1]
        assert f"МАФИЯ #{room_id}" in view
        assert "1/8" in view and "от 5" in view            # max=8, min=5
        assert any("Присоединиться" in row[0] for row in spy.kb_texts())

        # вход в комнату (тот же сервис, что за кнопкой)
        joined, result = await services.rooms.join(room_id, guest.id)
        assert joined is not None
        again, msg = await services.rooms.join(room_id, guest.id)
        assert again is not None and "уже" in msg           # повторный вход — отказ
