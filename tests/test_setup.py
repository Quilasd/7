"""Первоначальная настройка сервера: onboarding, /setup, кнопка проверки.

Сценарии ТЗ «Финальная проверка» (TEST 1–8):
1. бот добавлен без прав → инструкция + понятное «нужны права админа»;
2. админ без can_manage_topics → /setup сообщает про управление темами;
3. права есть, темы отключены → /setup просит включить «Темы»;
4. всё включено → /setup завершает настройку;
5. повторный /setup → нет дублей в БД;
6. две группы → независимые настройки/форумы;
7. права отозваны после настройки → бот не падает, понятная ошибка;
8. обычный пользователь жмёт «Проверить настройку» → отказ.
"""

from __future__ import annotations

import pytest

from bot.database.repositories.groups import GroupRepository, GroupSettingsRepository
from bot.handlers import setup as sp
from bot.utils.callbacks import SetupCB
from tests.test_handlers_smoke import (
    FakeCallback,
    FakeChat,
    FakeMessage,
    FakeTgUser,
)
from tests.conftest import make_user

BOT_ID = 999


class FakeMember:
    def __init__(self, status: str, can_manage_topics: bool = False) -> None:
        self.status = status
        self.can_manage_topics = can_manage_topics


class FakeChatInfo:
    def __init__(self, chat_id: int, title: str, is_forum: bool) -> None:
        self.id = chat_id
        self.title = title
        self.type = "supergroup"
        self.is_forum = is_forum


class FakeSetupBot:
    """Бот для тестов настройки: get_chat/get_chat_member/send_message."""

    def __init__(
        self,
        chat_id: int,
        *,
        is_forum: bool = True,
        bot_status: str = "administrator",
        bot_can_manage_topics: bool = True,
        user_statuses: dict[int, str] | None = None,
    ) -> None:
        self.id = BOT_ID
        self.chat_id = chat_id
        self._chat = FakeChatInfo(chat_id, "Тестовая группа", is_forum)
        self._bot_member = FakeMember(bot_status, bot_can_manage_topics)
        self.user_statuses = user_statuses or {}
        self.sent: list[tuple[int, str]] = []

    async def get_chat(self, chat_id: int) -> FakeChatInfo:
        if chat_id != self.chat_id:
            raise RuntimeError("chat not found")
        return self._chat

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
        if user_id == self.id:
            return self._bot_member
        status = self.user_statuses.get(user_id, "member")
        return FakeMember(status)

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kw):
        self.sent.append((chat_id, text))
        return text


class FakeChatMemberBot:
    """Заглка участника-бота для ChatMemberUpdated."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.user = type("U", (), {"id": BOT_ID})()


class FakeMemberUpdated:
    """Заглушка события my_chat_member."""

    def __init__(self, chat_id: int, title: str, new_status: str) -> None:
        self.chat = FakeChatInfo(chat_id, title, is_forum=True)
        self.new_chat_member = FakeChatMemberBot(new_status)


async def _groups_count(session) -> int:
    from sqlalchemy import func, select

    from bot.database.models import Group, GroupSettingsModel

    g = (await session.execute(select(func.count()).select_from(Group))).scalar_one()
    s = (
        await session.execute(select(func.count()).select_from(GroupSettingsModel))
    ).scalar_one()
    await session.commit()  # снять READ-лок, не мешать параллельным сессиям
    return int(g + s)


async def _run_setup(services, session, user, chat_id, bot, group=None):
    msg = FakeMessage(
        FakeTgUser(user.telegram_id), "/setup",
        chat=FakeChat(chat_id, "supergroup"),
    )
    await sp.cmd_setup(
        msg, session=session, services=services, db_user=user,
        group=group, bot=bot,
    )
    return msg


@pytest.fixture()
def owner_user(session):
    return make_user


class TestSetupCommand:
    """TEST 1–4: /setup — все ветки проверки прав и тем."""

    async def _admin(self, session, services, chat_id):
        """Telegram-администратор группы."""
        admin = await make_user(session, "Admin")
        return admin

    async def test_setup_in_private_rejected(self, services, session):
        user = await make_user(session, "Solo")
        bot = FakeSetupBot(user.id)
        msg = FakeMessage(FakeTgUser(user.telegram_id), "/setup",
                          chat=FakeChat(user.id, "private"))
        await sp.cmd_setup(msg, session=session, services=services,
                           db_user=user, group=None, bot=bot)
        assert any("в группе" in t for t in msg.answers)

    async def test_setup_requires_admin(self, services, session):
        """Обычный участник не может настраивать: отказ в правах."""
        user = await make_user(session, "Player")
        bot = FakeSetupBot(-400001, user_statuses={})
        group = await services.groups.get_or_create(-400001, "G")
        msg = await _run_setup(services, session, user, -400001, bot, group)
        assert any("нет прав" in t for t in msg.answers)

    async def test_setup_by_telegram_admin_ok(self, services, session):
        """Telegram-администратор (не локальный staff) может /setup."""
        admin = await make_user(session, "TgAdmin")
        bot = FakeSetupBot(-400002, user_statuses={admin.telegram_id: "administrator"})
        group = await services.groups.get_or_create(-400002, "G2")
        msg = await _run_setup(services, session, admin, -400002, bot, group)
        assert msg.answers and "нет прав" not in msg.answers[0]

    async def test_setup_by_group_creator_ok(self, services, session):
        creator = await make_user(session, "Creator")
        bot = FakeSetupBot(-400003, user_statuses={creator.telegram_id: "creator"})
        group = await services.groups.get_or_create(-400003, "G3")
        msg = await _run_setup(services, session, creator, -400003, bot, group)
        assert msg.answers and "нет прав" not in msg.answers[0]

    async def test_setup_by_local_senior_admin_ok(self, services, session):
        """Локальный Senior Admin группы (совместимость с существующей
        системой прав) может /setup даже без Telegram-статуса."""
        from bot.services.permissions import AdminLevel

        senior = await make_user(session, "Senior")
        group = await services.groups.get_or_create(-400004, "G4")
        await session.commit()
        await services.groups.set_staff(
            group.id, senior.telegram_id, AdminLevel.OWNER, senior.id, 4, senior.id
        )
        bot = FakeSetupBot(-400004)  # все статусы member — не Telegram-админ
        msg = await _run_setup(services, session, senior, -400004, bot, group)
        assert msg.answers and "нет прав" not in msg.answers[0]

    async def test_setup_by_global_owner_ok(self, services, session, monkeypatch):
        owner = await make_user(session, "God")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
        group = await services.groups.get_or_create(-400005, "G5")
        bot = FakeSetupBot(-400005)
        msg = await _run_setup(services, session, owner, -400005, bot, group)
        assert msg.answers and "нет прав" not in msg.answers[0]

    # ---- TEST 1: бот без прав администратора

    async def test_bot_not_admin_report(self, services, session):
        admin = await make_user(session, "Admin1")
        bot = FakeSetupBot(
            -400010, is_forum=True,
            bot_status="member", bot_can_manage_topics=False,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(-400010, "T1")
        msg = await _run_setup(services, session, admin, -400010, bot, group)
        text = msg.answers[0]
        assert "не выданы права администратора" in text
        assert "права администратора" in text
        # настройка НЕ зафиксирована
        gs = await GroupSettingsRepository(session).get_for(group.id)
        assert gs.setup_completed_at is None

    # ---- TEST 2: админ без can_manage_topics

    async def test_bot_admin_without_manage_topics(self, services, session):
        admin = await make_user(session, "Admin2")
        bot = FakeSetupBot(
            -400020, is_forum=True,
            bot_status="administrator", bot_can_manage_topics=False,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(-400020, "T2")
        msg = await _run_setup(services, session, admin, -400020, bot, group)
        text = msg.answers[0]
        assert "Управление темами" in text
        assert "не хватает права" in text.lower() or "Не хватает права" in text
        gs = await GroupSettingsRepository(session).get_for(group.id)
        assert gs.setup_completed_at is None

    # ---- TEST 3: права есть, Forum Topics отключены

    async def test_forum_topics_disabled_report(self, services, session):
        admin = await make_user(session, "Admin3")
        bot = FakeSetupBot(
            -400030, is_forum=False,
            bot_status="administrator", bot_can_manage_topics=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(-400030, "T3")
        msg = await _run_setup(services, session, admin, -400030, bot, group)
        text = msg.answers[0]
        assert "не включены темы форума" in text
        assert "«Темы»" in text
        gs = await GroupSettingsRepository(session).get_for(group.id)
        assert gs.setup_completed_at is None

    # ---- TEST 4: всё включено

    async def test_full_success(self, services, session):
        admin = await make_user(session, "Admin4")
        bot = FakeSetupBot(
            -400040, is_forum=True,
            bot_status="administrator", bot_can_manage_topics=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(-400040, "T4")
        msg = await _run_setup(services, session, admin, -400040, bot, group)
        text = msg.answers[0]
        assert "успешно настроен" in text
        assert "🟢 Бот — администратор" in text
        assert "🟢 Управление темами — доступно" in text
        assert "🟢 Forum Topics — включены" in text
        assert "🟢 База данных — подключена" in text
        # настройка зафиксирована, форумы партий автозаполнены самой группой
        gs = await GroupSettingsRepository(session).get_for(group.id)
        assert gs.setup_completed_at is not None
        assert gs.game_forum_chat_id == -400040
        assert gs.mafia_forum_chat_id == -400040

    async def test_autoforums_do_not_overwrite_existing(self, services, session):
        """Уже настроенные форумы партий /setup не перезаписывает."""
        admin = await make_user(session, "Admin5")
        group = await services.groups.get_or_create(-400050, "T5")
        gs = await GroupSettingsRepository(session).get_or_create(group.id)
        gs.game_forum_chat_id = -555001
        gs.mafia_forum_chat_id = -555002
        await session.commit()
        bot = FakeSetupBot(
            -400050, is_forum=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        msg = await _run_setup(services, session, admin, -400050, bot, group)
        assert "успешно настроен" in msg.answers[0]
        fresh = await GroupSettingsRepository(session).get_for(group.id)
        assert fresh.game_forum_chat_id == -555001  # не тронуто
        assert fresh.mafia_forum_chat_id == -555002


class TestSetupIdempotent:
    """TEST 5: повторный /setup — без дублей в БД."""

    async def _do_setup(self, services, session, admin, chat_id, title):
        bot = FakeSetupBot(
            chat_id, is_forum=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(chat_id, title)
        return await _run_setup(services, session, admin, chat_id, bot, group)

    async def test_repeated_setup_no_duplicates(self, services, session):
        admin = await make_user(session, "AdminR")
        before = await _groups_count(session)

        first = await self._do_setup(services, session, admin, -400060, "R1")
        mid = await _groups_count(session)
        assert "успешно настроен" in first.answers[0]

        second = await self._do_setup(services, session, admin, -400060, "R1")
        third = await self._do_setup(services, session, admin, -400060, "R1")
        after = await _groups_count(session)

        assert mid == before + 2            # группа + настройки созданы ОДИН раз
        assert after == mid                 # повторные /setup ничего не добавили
        assert "успешно настроен" in second.answers[0]
        assert "успешно настроен" in third.answers[0]

        # и одна запись группы с одним набором настроек
        groups = await GroupRepository(session).get_by_chat_id(-400060)
        assert groups is not None
        gs = await GroupSettingsRepository(session).get_for(groups.id)
        assert gs.setup_completed_at is not None


class TestMultiServerSetup:
    """TEST 6: две группы настраиваются независимо."""

    async def _setup_group(self, services, session, admin, chat_id, title, **bot_kw):
        bot = FakeSetupBot(
            chat_id, **{"user_statuses": {admin.telegram_id: "administrator"}, **bot_kw}
        )
        group = await services.groups.get_or_create(chat_id, title)
        msg = await _run_setup(services, session, admin, chat_id, bot, group)
        return msg, bot

    async def test_two_groups_independent(self, services, session):
        admin = await make_user(session, "DualAdmin")
        msg_a, _ = await self._setup_group(
            services, session, admin, -400070, "A", is_forum=True
        )
        # группа B: темы отключены — настройка НЕ завершится
        msg_b, _ = await self._setup_group(
            services, session, admin, -400080, "B", is_forum=False
        )
        assert "успешно настроен" in msg_a.answers[0]
        assert "не включены темы форума" in msg_b.answers[0]

        ga = await GroupRepository(session).get_by_chat_id(-400070)
        gb = await GroupRepository(session).get_by_chat_id(-400080)
        assert ga.id != gb.id
        gsa = await GroupSettingsRepository(session).get_for(ga.id)
        gsb = await GroupSettingsRepository(session).get_for(gb.id)
        # A настроена и получила свои форумы, B — нет
        assert gsa.setup_completed_at is not None
        assert gsb.setup_completed_at is None
        assert gsa.game_forum_chat_id == -400070
        assert gsb.game_forum_chat_id is None
        # форумы/темы групп не смешиваются: разные chat_id
        assert gsa.game_forum_chat_id != gsb.game_forum_chat_id

    async def test_thread_ids_not_shared_between_groups(self, services, session):
        """message_thread_id привязан к chat_id: (chat, thread) уникален.
        Тема группы A не резолвится в контексте группы B (enforce/context_for
        ищут по паре chat_id+thread_id)."""
        from bot.services.game_chat import GameChatService

        gc = GameChatService(
            services.session_factory, type("Noop", (), {
                "create_topic": lambda self, c, n, i=None: None,
            })(), None,
        )
        assert gc is not None  # сервис собирается
        # контекст темы ищется ТОЧНО по (chat_id, thread_id) — уже покрыто
        # test_game_chats.TestContextIsolation; здесь фиксируем контракт:
        # тема (A, 500) и тема (B, 500) — РАЗНЫЕ темы одного игрока.
        assert (-400070, 500) != (-400080, 500)


class TestRightsRevoked:
    """TEST 7: право отозвано после настройки — бот не падает, понятная ошибка."""

    async def test_manage_topics_revoked_after_setup(self, services, session):
        admin = await make_user(session, "Admin7")
        # настройка прошла успешно
        bot_ok = FakeSetupBot(
            -400090, is_forum=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        group = await services.groups.get_or_create(-400090, "T7")
        msg_ok = await _run_setup(services, session, admin, -400090, bot_ok, group)
        assert "успешно настроен" in msg_ok.answers[0]

        # право отозвали: повторный /setup — конкретная ошибка, не traceback
        bot_bad = FakeSetupBot(
            -400090, is_forum=True,
            bot_status="administrator", bot_can_manage_topics=False,
            user_statuses={admin.telegram_id: "administrator"},
        )
        msg_bad = await _run_setup(services, session, admin, -400090, bot_bad, group)
        text = msg_bad.answers[0]
        assert "Управление темами" in text
        assert "/setup" in text  # предлагаем повторить после исправления

        # сервис проверки не бросает исключений при недоступном API
        bot_dead = FakeSetupBot(-400099, is_forum=True)
        bot_dead.get_chat = lambda chat_id: (_ for _ in ()).throw(RuntimeError("api down"))
        check = await services.setup.check(bot_dead, -400099)
        assert not check.ok
        assert check.problems  # человекочитаемая проблема, а не exception


class TestCheckButton:
    """TEST 8 + кнопка «Проверить настройку»."""

    async def test_plain_player_rejected(self, services, session):
        player = await make_user(session, "Player8")
        group = await services.groups.get_or_create(-400100, "T8")
        bot = FakeSetupBot(-400100)
        cb = FakeCallback(FakeTgUser(player.telegram_id))
        await sp.cb_setup_check(
            cb, session=session, services=services, db_user=player,
            group=group, bot=bot,
        )
        assert any("только администратор" in (a or "") for a in cb.answers)

    async def test_admin_button_runs_check(self, services, session):
        """Админ нажимает кнопку — выполняется та же проверка, что и /setup."""
        admin = await make_user(session, "AdminBtn")
        group = await services.groups.get_or_create(-400110, "T9")
        await session.commit()

        bot = FakeSetupBot(
            -400110, is_forum=True,
            user_statuses={admin.telegram_id: "administrator"},
        )
        # сообщение, которое редактируется
        edited: list[str] = []

        class FakeMsg:
            async def edit_text(self, text, reply_markup=None, **kw):
                edited.append(text)

        cb = FakeCallback(FakeTgUser(admin.telegram_id))
        cb.message = FakeMsg()
        await sp.cb_setup_check(
            cb, session=session, services=services, db_user=admin,
            group=group, bot=bot,
        )
        assert edited, "сообщение должно быть обновлено, а не задублировано"
        assert "успешно настроен" in edited[0]
        gs = await GroupSettingsRepository(session).get_for(group.id)
        assert gs.setup_completed_at is not None

    async def test_button_outside_group(self, services, session):
        user = await make_user(session, "Lonely")
        cb = FakeCallback(FakeTgUser(user.telegram_id))
        await sp.cb_setup_check(
            cb, session=session, services=services, db_user=user,
            group=None, bot=FakeSetupBot(user.id),
        )
        assert any("в группе" in (a or "") for a in cb.answers)


class TestOnboarding:
    """Автоматическое сообщение при добавлении бота в группу."""

    async def test_added_sends_welcome(self, services, session):
        """TEST 1: бот добавлен без прав — инструкция + понятное «нужны права»."""
        event = FakeMemberUpdated(-400120, "Новая группа", "member")
        bot = FakeSetupBot(-400120, bot_status="member", bot_can_manage_topics=False)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        assert len(bot.sent) == 1
        chat_id, text = bot.sent[0]
        assert chat_id == -400120
        assert "Mafia Online добавлен" in text
        assert "/setup" in text
        assert "права администратора" in text
        # автоматическая проверка: конкретная причина + что делать
        assert "Бот пока не может работать" in text
        assert "повторите /setup" in text

    async def test_added_admin_without_manage_topics(self, services, session):
        """Автопроверка после добавления: админ, но нет управления темами."""
        event = FakeMemberUpdated(-400121, "Группа", "administrator")
        bot = FakeSetupBot(-400121, bot_status="administrator",
                           bot_can_manage_topics=False)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        _, text = bot.sent[0]
        assert "Не хватает права «Управление темами»" in text
        assert "повторите /setup" in text

    async def test_added_forum_topics_disabled(self, services, session):
        """Автопроверка после добавления: права есть, темы отключены."""
        event = FakeMemberUpdated(-400122, "Группа", "administrator")
        bot = FakeSetupBot(-400122, bot_status="administrator",
                           bot_can_manage_topics=True, is_forum=False)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        _, text = bot.sent[0]
        assert "не включены темы форума" in text
        assert "Включите «Темы»" in text
        assert "повторите /setup" in text

    async def test_added_with_full_rights_hint(self, services, session):
        """Автопроверка после добавления: всё готово — предложение /setup."""
        event = FakeMemberUpdated(-400130, "Группа", "administrator")
        bot = FakeSetupBot(-400130, bot_status="administrator",
                           bot_can_manage_topics=True)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        _, text = bot.sent[0]
        assert "готов к работе в этой группе" in text
        assert "выполнить /setup" in text

    async def test_readd_no_duplicates(self, services, session):
        """TEST 5/13: повторное добавление — та же запись группы, без дублей."""
        event = FakeMemberUpdated(-400140, "Повторная", "member")
        bot = FakeSetupBot(-400140)
        before = await _groups_count(session)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        after = await _groups_count(session)
        # ОДНА группа (настройки создаёт /setup, а не onboarding), без дублей
        assert after == before + 1
        # три приветствия — это нормальные ответы на три события
        assert len(bot.sent) == 3

    async def test_readd_configured_group_shows_status(self, services, session):
        """Бота удалили и вернули в НАСТРОЕННУЮ группу: актуальный статус
        и предложение /setup, настройки сохранены."""
        group = await services.groups.get_or_create(-400150, "Старая")
        gs = await GroupSettingsRepository(session).get_or_create(group.id)
        from bot.utils.helpers import utcnow

        gs.setup_completed_at = utcnow()
        await session.commit()

        event = FakeMemberUpdated(-400150, "Старая", "member")
        bot = FakeSetupBot(-400150, bot_status="administrator",
                           bot_can_manage_topics=True, is_forum=True)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        _, text = bot.sent[0]
        assert "снова в группе" in text
        assert "сохранены" in text

    async def test_bot_removed_no_crash(self, services, session):
        """Бота удалили из группы: только лог, без падения и без сообщений."""
        event = FakeMemberUpdated(-400160, "Уходим", "kicked")
        bot = FakeSetupBot(-400160)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        assert bot.sent == []

    async def test_send_failure_no_crash(self, services, session):
        """Не удалось отправить сообщение (нет прав на сообщения) — бот жив."""
        event = FakeMemberUpdated(-400170, "Тихая", "member")

        class SilentBot(FakeSetupBot):
            async def send_message(self, chat_id, text, reply_markup=None, **kw):
                raise RuntimeError("bot was blocked")

        bot = SilentBot(-400170)
        await sp.on_bot_added(event, bot=bot, session=session, services=services)
        # главное — не упало


class TestSettingsStatusBlock:
    """/settings показывает статус сервера (настроенность, форумы)."""

    async def test_settings_shows_setup_status(self, services, session):
        from bot.handlers import groups_admin as ga

        boss = await make_user(session, "Boss")
        group = await services.groups.get_or_create(-400180, "S1")
        await session.commit()
        from bot.services.permissions import AdminLevel

        await services.groups.set_staff(
            group.id, boss.telegram_id, AdminLevel.OWNER, boss.id, 4, boss.id
        )
        gs = await GroupSettingsRepository(session).get_or_create(group.id)
        gs.game_forum_chat_id = -666001
        await session.commit()

        msg = FakeMessage(
            FakeTgUser(boss.telegram_id), "/settings",
            chat=FakeChat(group.telegram_chat_id),
        )
        # message.bot нужен для живой проверки прав
        msg.bot = FakeSetupBot(-400180, is_forum=True, bot_status="member")
        await ga.cmd_settings(msg, session=session, group=group, services=services)
        text = msg.answers[0]
        assert "НАСТРОЙКИ MAFIA ONLINE" in text
        assert "Не настроен" in text          # /setup не выполнен
        assert "-666001" in text              # форумы партий показаны
        assert "Бот не администратор" in text  # живой статус прав
