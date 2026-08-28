"""Аудит системы друзей: regression-тесты найденных проблем (15 сценариев ТЗ).

Найденное и закрытое здесь:
1. /favorite позволял добавить НЕдруга — обход на уровне SocialService.favorite
   (теперь правило «избранное только для друзей» живёт в сервисе, его не
   обойти ни хендлером, ни колбэком).
2. /unfriend оставлял «призрачное» избранное — теперь снимается в обе стороны;
   /favorites дополнительно показывает только действующих друзей.
3. /invite падал (TypeError: room.player_count — метод — сравнивался с int):
   хендлер молча умирал, пользователь не получал ответа. Плюс добавлены
   проверки «цель уже в другой комнате / в игре» и «тестовый бот».
4. Гонки дублей (check-then-add) — IntegrityError теперь даёт человеческий
   ответ вместо 500; _become_friends идемпотентен.

Зафиксировано существующее поведение (НЕ менялось):
- дружба + игнор сосуществуют: игнор по документации применяется к /invite;
- приглашения — stateless-уведомления (нет записей в БД): после старта/конца
  игры ничего не чистится, /join просто откажет «комната недоступна»;
- отношения ГЛОБАЛЬНЫЕ (друзья/избранное/игнор без group_id — см. TEST 15).
"""

from __future__ import annotations

import inspect

from bot.database.models import (
    FavoritePlayer,
    FriendRequest,
    Friendship,
    User,
    UserBlock,
)
from bot.database.repositories.social import (
    FavoriteRepository,
    FriendRequestRepository,
    FriendshipRepository,
    UserBlockRepository,
)
from bot.handlers.social import cmd_invite
from tests.conftest import make_user, make_room


async def _befriend(services, a, b) -> None:
    ok, msg = await services.social.send_request(a.id, b.id)
    assert ok, msg
    ok, msg = await services.social.accept_request(b.id, a.id)
    assert ok, msg


class FakeMsg:
    """Минимальный Message для вызова хендлера как это делает aiogram."""

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.reply_to_message = None
        self.from_user = None

    async def answer(self, text: str, reply_markup=None, **kwargs) -> None:
        self.answers.append(text)


class FakeCmd:
    def __init__(self, args: str | None) -> None:
        self.args = args
        self.command = "invite"


async def _call_invite(handler, message, command, session, services, db_user):
    """Вызов cmd_invite ровно с параметрами его сигнатуры (DI по имени)."""
    sig = inspect.signature(handler)
    data = dict(message=message, command=command, session=session,
                services=services, db_user=db_user)
    await handler(**{k: v for k, v in data.items() if k in sig.parameters})


class TestFavoriteRequiresFriendship:
    """TEST 1, 2, 3, 4, 5: избранное — подмножество друзей."""

    async def test_favorite_of_non_friend_refused(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        ok, msg = await services.social.favorite(a.id, b.id)
        assert not ok
        assert "не является вашим другом" in msg
        # и не появилось «мусорной» записи в БД
        async with services.session_factory() as s:
            assert await FavoriteRepository(s).list_ids(a.id) == []

    async def test_favorite_of_friend_works(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        ok, msg = await services.social.favorite(a.id, b.id)
        assert ok, msg
        assert [u.id for u in await services.social.favorites_of(a.id)] == [b.id]

    async def test_favorite_twice_no_duplicate(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        assert (await services.social.favorite(a.id, b.id))[0]
        ok, msg = await services.social.favorite(a.id, b.id)
        assert not ok and "уже" in msg
        async with services.session_factory() as s:
            assert len(await FavoriteRepository(s).list_ids(a.id)) == 1

    async def test_unfavorite_and_repeat(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        await services.social.favorite(a.id, b.id)
        ok, _ = await services.social.unfavorite(a.id, b.id)
        assert ok and await services.social.favorites_of(a.id) == []
        # повторный unfavorite — корректный отказ, не ошибка
        ok, msg = await services.social.unfavorite(a.id, b.id)
        assert not ok and "нет в избранном" in msg

    async def test_unfriend_removes_favorites_both_sides(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        await services.social.favorite(a.id, b.id)
        await services.social.favorite(b.id, a.id)

        ok, _ = await services.social.remove_friend(a.id, b.id)
        assert ok
        # призрачного избранного нет ни у одной стороны
        assert await services.social.favorites_of(a.id) == []
        assert await services.social.favorites_of(b.id) == []
        async with services.session_factory() as s:
            assert await FavoriteRepository(s).list_ids(a.id) == []
            assert await FavoriteRepository(s).list_ids(b.id) == []
        # обе стороны больше не друзья
        assert not await services.social.are_friends(a.id, b.id)
        assert not await services.social.are_friends(b.id, a.id)

    async def test_favorites_hides_legacy_ghost_rows(self, services, session):
        """Строка избранного, оставшаяся до введения правила (недруг),
        не показывается в /favorites."""
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        async with services.session_factory() as s:
            await FavoriteRepository(s).add(a.id, b.id)  # напрямую, как старый код
            await s.commit()
        assert await services.social.favorites_of(a.id) == []


class TestFriendRequests:
    """TEST 6, 7, 8, 9: заявки и unfriend."""

    async def test_duplicate_request_refused_no_db_dup(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        assert (await services.social.send_request(a.id, b.id))[0]
        ok, msg = await services.social.send_request(a.id, b.id)
        assert not ok and "уже отправлен" in msg
        async with services.session_factory() as s:
            reqs = await FriendRequestRepository(s).pending_to(b.id)
            assert len(reqs) == 1

    async def test_accept_nonexistent_request_refused(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        ok, msg = await services.social.accept_request(b.id, a.id)  # заявки нет
        assert not ok and "нет" in msg
        assert not await services.social.are_friends(a.id, b.id)

    async def test_decline_removes_request_and_resend_allowed(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await services.social.send_request(a.id, b.id)
        ok, msg = await services.social.decline_request(b.id, a.id)
        assert ok and "отклонён" in msg
        # не стали друзьями, заявки не осталось
        assert not await services.social.are_friends(a.id, b.id)
        async with services.session_factory() as s:
            assert await FriendRequestRepository(s).pending_to(b.id) == []
        # повторная заявка разрешена и не создаёт дублей
        assert (await services.social.send_request(a.id, b.id))[0]
        ok, _ = await services.social.send_request(a.id, b.id)
        assert not ok
        async with services.session_factory() as s:
            assert len(await FriendRequestRepository(s).pending_to(b.id)) == 1

    async def test_repeat_unfriend_is_safe(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        assert (await services.social.remove_friend(a.id, b.id))[0]
        ok, msg = await services.social.remove_friend(a.id, b.id)
        assert not ok and "нет в друзьях" in msg

    async def test_request_to_test_bot_refused(self, services, session):
        a = await make_user(session, "A")
        bot_user = User(telegram_id=-900000001, display_name="TestPlayer1", is_test=True)
        session.add(bot_user)
        await session.commit()
        ok, msg = await services.social.send_request(a.id, bot_user.id)
        assert not ok and "тестовый" in msg


class TestIgnore:
    """TEST 10, 11: игнор-лист."""

    async def test_block_unblock_and_lists(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        ok, _ = await services.social.block(a.id, b.id)
        assert ok and await services.social.is_blocked(a.id, b.id)
        # дубль игнора
        ok, msg = await services.social.block(a.id, b.id)
        assert not ok and "уже" in msg
        async with services.session_factory() as s:
            assert len(await UserBlockRepository(s).blocked_ids(a.id)) == 1
        # виден в списке
        assert [u.id for u in await services.social.blocked_users(a.id)] == [b.id]
        # unignore
        ok, _ = await services.social.unblock(a.id, b.id)
        assert ok and not await services.social.is_blocked(a.id, b.id)
        assert await services.social.blocked_users(a.id) == []
        # повторный unignore
        ok, msg = await services.social.unblock(a.id, b.id)
        assert not ok and "нет в игнор-листе" in msg

    async def test_block_coexists_with_friendship_per_docs(self, services, session):
        """Существующее правило: дружба и игнор сосуществуют, игнор режет
        только /invite (см. docstring модуля social)."""
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        assert (await services.social.block(a.id, b.id))[0]
        # дружба не сломана, в друзьях и в игноре одновременно
        assert await services.social.are_friends(a.id, b.id)
        assert await services.social.is_blocked(a.id, b.id)
        # избранное друга, находящегося в игноре, остаётся (он всё ещё друг)
        assert (await services.social.favorite(a.id, b.id))[0]


class TestSelfActionsAndUnknownUsers:
    """TEST 12, 13: действия над собой и несуществующие пользователи."""

    async def test_self_actions_refused(self, services, session):
        a = await make_user(session, "A")
        assert not (await services.social.send_request(a.id, a.id))[0]
        assert not (await services.social.favorite(a.id, a.id))[0]
        assert not (await services.social.block(a.id, a.id))[0]
        assert not (await services.social.accept_request(a.id, a.id))[0]
        # unfriend себя — корректный отказ без ошибок
        ok, msg = await services.social.remove_friend(a.id, a.id)
        assert not ok and "нет в друзьях" in msg
        # /invite себя — отказ на уровне хендлера
        msg_obj, cmd = FakeMsg(), FakeCmd(str(a.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("самого себя" in t for t in msg_obj.answers)

    async def test_unknown_users_no_garbage(self, services, session):
        a = await make_user(session, "A")
        big_id = 10**9
        for method, expected in (
            (services.social.send_request, "не найден"),
            (services.social.favorite, "не найден"),
            (services.social.block, "не найден"),
        ):
            ok, msg = await method(a.id, big_id)
            assert not ok and expected in msg, (method, msg)
        # мусора в БД не появилось
        async with services.session_factory() as s:
            assert await FriendRequestRepository(s).pending_to(big_id) == []
            assert await FavoriteRepository(s).list_ids(a.id) == []
            assert await UserBlockRepository(s).blocked_ids(a.id) == []
            assert await FriendshipRepository(s).list_friends(a.id) == []


class TestInvite:
    """TEST 14: edge cases /invite — включая регрессию краша player_count."""

    async def test_invite_requires_open_room(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        msg_obj, cmd = FakeMsg(), FakeCmd(str(b.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("нет открытой комнаты" in t for t in msg_obj.answers)

    async def test_invite_friend_sends_notification(self, services, session):
        """До фикса хендлер умирал на room.player_count >= room.max_players
        (метод сравнивался с int -> TypeError) и не отвечал ничего."""
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await _befriend(services, a, b)
        await make_room(session, a, [a])
        msg_obj, cmd = FakeMsg(), FakeCmd(str(b.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("Приглашение отправлено" in t for t in msg_obj.answers)

    async def test_invite_target_in_other_room_refused(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await make_room(session, a, [a])
        await make_room(session, b, [b])            # B уже в своей комнате
        msg_obj, cmd = FakeMsg(), FakeCmd(str(b.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("уже в комнате" in t for t in msg_obj.answers)
        assert not any("Приглашение отправлено" in t for t in msg_obj.answers)

    async def test_invite_test_bot_refused(self, services, session):
        a = await make_user(session, "A")
        bot_user = User(telegram_id=-900000002, display_name="TestPlayer2", is_test=True)
        session.add(bot_user)
        await session.commit()
        await make_room(session, a, [a])
        msg_obj, cmd = FakeMsg(), FakeCmd(str(bot_user.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("тестовый" in t for t in msg_obj.answers)

    async def test_invite_ignored_either_way_refused(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        await make_room(session, a, [a])
        await services.social.block(b.id, a.id)      # B игнорирует A
        msg_obj, cmd = FakeMsg(), FakeCmd(str(b.telegram_id))
        await _call_invite(cmd_invite, msg_obj, cmd, session, services, a)
        assert any("не принимает" in t for t in msg_obj.answers)


class TestCrossServerScope:
    """TEST 15: отношения пользователей — ГЛОБАЛЬНЫЕ (архитектура).

    В таблицах friendship/favorite_players/user_blocks/friend_requests нет
    group_id: друзья/избранное/игнор не зависят от чата, в котором выполнена
    команда. Проверяем, что это по-прежнему так (случайный занос chat_id
    в социальные модели был бы регрессией)."""

    async def test_social_models_have_no_group_columns(self):
        for model in (Friendship, FavoritePlayer, UserBlock, FriendRequest):
            columns = set(model.__table__.columns.keys())
            assert not any("group" in name or "chat" in name for name in columns), model

    async def test_friendship_visible_regardless_of_groups(self, services, session):
        a = await make_user(session, "A")
        b = await make_user(session, "B")
        # группы существуют, но отношение создаётся без чата и живёт глобально
        g1 = await services.groups.get_or_create(-100970, "G1")
        g2 = await services.groups.get_or_create(-100980, "G2")
        await _befriend(services, a, b)
        assert g1.id != g2.id
        assert await services.social.are_friends(a.id, b.id)
        assert [u.id for u in await services.social.friends_of(a.id)] == [b.id]
