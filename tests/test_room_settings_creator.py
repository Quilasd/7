"""Regression: создатель комнаты не мог менять её настройки (путаница ID).

Баг (живой Telegram): создатель комнаты нажимал ➖ на роли и получал
«Настройки меняет только создатель». Причина — в handlers/rooms.py вызов
`services.rooms.update_settings(room_id, callback.from_user.id, ...)` передавал
TELEGRAM ID, а сервис сравнивает его с `room.creator_id` — внутренним DB
users.id (from_user.id=777000111 vs room.creator_id=1 — воспроизведено).
Та же ошибка была в таймерах/правиле ничьей/раскрытии ролей и в перерисовке
комнаты после изменения.

Фикс: хендлеры передают db_user.id (как close/kick/leave/start и раньше).
Правило НЕ ослаблено: менять настройки может только создатель и только
до старта игры (проверки в RoomService.update_settings не менялись).
"""

from __future__ import annotations

import inspect

import bot.handlers.rooms as rm
from bot.database.models import Room, RoomStatus
from bot.database.repositories.rooms import RoomRepository
from bot.utils.callbacks import RoomCB
from tests.conftest import make_user


class FakeCB:
    """from_user.id — ТЕЛЕГРАМ ID (как в реальном callback), не DB id."""

    def __init__(self, telegram_id: int) -> None:
        self.answers: list[str] = []
        self.message = None
        self.from_user = type("U", (), {"id": telegram_id})()

    async def answer(self, text: str | None = None, show_alert: bool = False, **kw) -> bool:
        self.answers.append(text or "")
        return True


async def call(handler, **data):
    sig = inspect.signature(handler)
    await handler(**{k: v for k, v in data.items() if k in sig.parameters})


async def _make_room(services, session, creator, *, group_id=None, roles=None):
    room, msg = await services.rooms.create_room(
        creator_user_id=creator.id, name="Комната теста", max_players=8, min_players=4,
        is_private=False, password=None,
        roles=roles or {"mafia": 1, "detective": 1, "doctor": 1},
        group_id=group_id,
    )
    assert room is not None, msg
    await session.commit()
    return room


async def _press(services, db_user, room_id, role_id, delta):
    action = "roleinc" if delta > 0 else "roledec"
    cb = FakeCB(db_user.telegram_id)  # реальный Telegram ID в from_user
    await call(rm.cb_role_inc if delta > 0 else rm.cb_role_dec,
               callback=cb, callback_data=RoomCB(action=action, room_id=room_id, value=role_id),
               services=services, state=None, db_user=db_user)
    return cb


async def _roles_of(services, room_id) -> dict:
    async with services.session_factory() as s:
        room = await RoomRepository(s).get(room_id)
        return dict(room.settings["roles"])


class TestCreatorCanChangeRoomSettings:
    async def test_ids_telegram_and_db_differ_yet_creator_passes(self, services, session, monkeypatch):
        """Ядро бага: telegram_id != db id, но создатель проходит проверку."""
        creator = await make_user(session, "Owner")
        assert creator.telegram_id != creator.id   # фикстура гарантирует разные ID
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)

        cb = await _press(services, creator, room.id, "doctor", -1)
        assert not any("создатель" in a.lower() for a in cb.answers), cb.answers
        assert await _roles_of(services, room.id) == {"mafia": 1, "detective": 1, "doctor": 0}

    async def test_creator_can_increase_role(self, services, session, monkeypatch):
        creator = await make_user(session, "Owner")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)

        cb = await _press(services, creator, room.id, "mafia", +1)
        assert any(a == "Изменено" for a in cb.answers)
        assert (await _roles_of(services, room.id))["mafia"] == 2

    async def test_creator_can_decrease_role(self, services, session, monkeypatch):
        creator = await make_user(session, "Owner")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator,
                                roles={"mafia": 2, "detective": 1, "doctor": 1})
        cb = await _press(services, creator, room.id, "mafia", -1)
        assert any(a == "Изменено" for a in cb.answers)
        assert (await _roles_of(services, room.id))["mafia"] == 1

    async def test_validation_still_applies(self, services, session, monkeypatch):
        """Бизнес-валидация не ослаблена: последнюю мафию снять нельзя."""
        creator = await make_user(session, "Owner")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)
        cb = await _press(services, creator, room.id, "mafia", -1)
        assert any("мафия" in a for a in cb.answers)
        assert (await _roles_of(services, room.id))["mafia"] == 1


class TestCreatorDeniedCases:
    async def test_non_creator_denied(self, services, session, monkeypatch):
        creator = await make_user(session, "Owner")
        stranger = await make_user(session, "Stranger")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)

        cb = await _press(services, stranger, room.id, "doctor", -1)
        assert any("только создатель" in a.lower() for a in cb.answers)
        assert (await _roles_of(services, room.id))["doctor"] == 1   # не изменилось

    async def test_creator_denied_after_game_started(self, services, session, monkeypatch):
        creator = await make_user(session, "Owner")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)
        async with services.session_factory() as s:
            (await RoomRepository(s).get(room.id)).status = RoomStatus.PLAYING.value
            await s.commit()

        cb = await _press(services, creator, room.id, "doctor", -1)
        assert any("после старта" in a.lower() for a in cb.answers)
        assert (await _roles_of(services, room.id))["doctor"] == 1


class TestCreatorScopeVariants:
    """Создатель определяется корректно для всех трёх путей создания."""

    async def test_group_wizard_room_creator(self, services, session, monkeypatch):
        """Комната, созданная визардом ИЗ ГРУППЫ (group_id передан)."""
        creator = await make_user(session, "Owner")
        group = await services.groups.get_or_create(-1002000, "mafia")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator, group_id=group.id)
        assert room.group_id == group.id

        cb = await _press(services, creator, room.id, "doctor", -1)
        assert not any("создатель" in a.lower() for a in cb.answers), cb.answers
        assert (await _roles_of(services, room.id))["doctor"] == 0

    async def test_createroom_command_room_creator(self, services, session, monkeypatch):
        """Комната, созданная /createroom (create_room_in_group)."""
        creator = await make_user(session, "Owner")
        group = await services.groups.get_or_create(-1002100, "mafia")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room, _ = await services.groups.create_room_in_group(group.id, creator.id)

        cb = await _press(services, creator, room.id, "detective", -1)
        assert not any("создатель" in a.lower() for a in cb.answers), cb.answers
        assert (await _roles_of(services, room.id))["detective"] == 0

    async def test_private_wizard_room_creator(self, services, session, monkeypatch):
        """Комната, созданная визардом из ЛС (group_id=None)."""
        creator = await make_user(session, "Owner")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator, group_id=None)
        assert room.group_id is None

        cb = await _press(services, creator, room.id, "doctor", -1)
        assert not any("создатель" in a.lower() for a in cb.answers), cb.answers
        assert (await _roles_of(services, room.id))["doctor"] == 0


class TestOtherSettingsButtonsSameFix:
    """Таймеры/правило ничьей/раскрытие — та же путаница ID, тот же фикс."""

    async def test_timer_tie_reveal_by_creator(self, services, session, monkeypatch):
        creator = await make_user(session, "Owner")
        stranger = await make_user(session, "Stranger")
        async def fake_edit(cb, text, kb=None): pass
        monkeypatch.setattr(rm, "edit_or_answer", fake_edit)
        room = await _make_room(services, session, creator)

        # экран таймеров собирается без ValueError (value без ":")
        from bot.keyboards.room import timer_adjust_kb
        timer_rows = [[b.callback_data for b in row] for row in timer_adjust_kb(room.id).inline_keyboard]
        assert any("night.+30" in cd for row in timer_rows for cd in row)

        # таймер: создатель меняет, посторонний — отказ
        cb = FakeCB(creator.telegram_id)
        await call(rm.cb_room_timer_set, callback=cb,
                   callback_data=RoomCB(action="timer", room_id=room.id, value="night.+30"),
                   services=services, db_user=creator)
        assert not any("создатель" in a.lower() for a in cb.answers), cb.answers
        cb2 = FakeCB(stranger.telegram_id)
        await call(rm.cb_room_timer_set, callback=cb2,
                   callback_data=RoomCB(action="timer", room_id=room.id, value="night.+30"),
                   services=services, db_user=stranger)
        assert any("только создатель" in a.lower() for a in cb2.answers)

        # правило ничьей: создатель переключает
        cb3 = FakeCB(creator.telegram_id)
        await call(rm.cb_room_tie, callback=cb3,
                   callback_data=RoomCB(action="tie", room_id=room.id),
                   services=services, db_user=creator)
        assert not any("Недоступно" in a for a in cb3.answers), cb3.answers
        async with services.session_factory() as s:
            fresh = await RoomRepository(s).get(room.id)
            assert fresh.settings["tie_rule"] == "no_death"   # переключилось с revote

        # раскрытие ролей: создатель переключает
        cb4 = FakeCB(creator.telegram_id)
        await call(rm.cb_room_reveal, callback=cb4,
                   callback_data=RoomCB(action="reveal", room_id=room.id),
                   services=services, db_user=creator)
        assert not any("Недоступно" in a for a in cb4.answers), cb4.answers
        async with services.session_factory() as s:
            fresh = await RoomRepository(s).get(room.id)
            assert fresh.settings["reveal_roles_on_death"] is False
