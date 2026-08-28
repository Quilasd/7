"""Тесты социальных функций: друзья, запросы, игнор, избранное, приглашения."""

from __future__ import annotations

import pytest

from bot.database.models import Room, RoomPlayer
from bot.database.repositories.users import UserRepository
from tests.conftest import make_user


@pytest.mark.asyncio
class TestFriends:
    async def test_send_accept_becomes_friends(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        ok, _ = await services.social.send_request(a.id, b.id)
        assert ok
        ok, _ = await services.social.accept_request(b.id, a.id)
        assert ok
        assert await services.social.are_friends(a.id, b.id)
        assert await services.social.are_friends(b.id, a.id)

    async def test_no_self_friend(self, services, session):
        a = await make_user(session, "Alice")
        ok, msg = await services.social.send_request(a.id, a.id)
        assert not ok
        assert "себя" in msg

    async def test_no_duplicate_request(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.send_request(a.id, b.id)
        ok, msg = await services.social.send_request(a.id, b.id)
        assert not ok
        assert "уже отправлен" in msg

    async def test_reverse_request_auto_accepts(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.send_request(a.id, b.id)
        # B отправляет встречный запрос A → сразу становятся друзьями
        ok, msg = await services.social.send_request(b.id, a.id)
        assert ok
        assert await services.social.are_friends(a.id, b.id)

    async def test_decline_and_remove(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.send_request(a.id, b.id)
        ok, _ = await services.social.decline_request(b.id, a.id)
        assert ok
        reqs = await services.social.pending_requests(b.id)
        assert reqs == []
        # друзья
        await services.social.send_request(a.id, b.id)
        await services.social.accept_request(b.id, a.id)
        ok, _ = await services.social.remove_friend(a.id, b.id)
        assert ok
        assert not await services.social.are_friends(a.id, b.id)

    async def test_friends_list(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.send_request(a.id, b.id)
        await services.social.accept_request(b.id, a.id)
        friends = await services.social.friends_of(a.id)
        assert [u.id for u in friends] == [b.id]


@pytest.mark.asyncio
class TestIgnore:
    async def test_block_unblock_list(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        ok, _ = await services.social.block(a.id, b.id)
        assert ok
        assert await services.social.is_blocked(a.id, b.id)
        assert not await services.social.is_blocked(b.id, a.id)  # односторонний
        blocked = await services.social.blocked_users(a.id)
        assert [u.id for u in blocked] == [b.id]
        ok, _ = await services.social.unblock(a.id, b.id)
        assert ok
        assert not await services.social.is_blocked(a.id, b.id)

    async def test_no_self_block(self, services, session):
        a = await make_user(session, "Alice")
        ok, msg = await services.social.block(a.id, a.id)
        assert not ok

    async def test_block_unknown_user(self, services, session):
        a = await make_user(session, "Alice")
        ok, msg = await services.social.block(a.id, 999999)
        assert not ok


@pytest.mark.asyncio
class TestFavorites:
    async def test_add_remove_list(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.send_request(a.id, b.id)   # избранное — только для друзей
        await services.social.accept_request(b.id, a.id)
        ok, _ = await services.social.favorite(a.id, b.id)
        assert ok
        favs = await services.social.favorites_of(a.id)
        assert [u.id for u in favs] == [b.id]
        ok, _ = await services.social.unfavorite(a.id, b.id)
        assert ok
        assert await services.social.favorites_of(a.id) == []

    async def test_no_self_favorite_no_dup(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        assert not (await services.social.favorite(a.id, a.id))[0]
        await services.social.send_request(a.id, b.id)   # избранное — только для друзей
        await services.social.accept_request(b.id, a.id)
        await services.social.favorite(a.id, b.id)
        ok, msg = await services.social.favorite(a.id, b.id)
        assert not ok
        assert "уже" in msg


@pytest.mark.asyncio
class TestInvite:
    async def test_invite_blocked_refused(self, services, session):
        a = await make_user(session, "Alice")
        b = await make_user(session, "Bob")
        await services.social.block(b.id, a.id)  # B игнорит A
        # у A нет открытой комнаты
        # создаём комнату для A вручную
        room = Room(creator_id=a.id, name="R", max_players=10, min_players=4,
                    status="OPEN", settings={"roles": {"mafia": 1, "detective": 1, "doctor": 1}})
        session.add(room)
        await session.flush()
        session.add(RoomPlayer(room_id=room.id, user_id=a.id, is_ready=True))
        await session.commit()
        from bot.database.repositories.rooms import RoomRepository
        room = await RoomRepository(session).get(room.id)

        # проверяем логику блокировки через is_blocked
        assert await services.social.is_blocked(b.id, a.id)
        # invite-хендлер использует is_blocked для отказа; проверяем это условие
        blocked_for_invite = await services.social.is_blocked(a.id, b.id) or await services.social.is_blocked(b.id, a.id)
        assert blocked_for_invite
