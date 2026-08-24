"""UserLookupService: поиск игрока для админ-команд.

Требования спека: Telegram ID (основной), @username, username без @,
reply/mention; неизвестный — None.
"""

from __future__ import annotations

from bot.services.lookup import UserLookupService
from tests.conftest import make_user


class TestUserLookup:
    async def test_by_telegram_id(self, session):
        user = await make_user(session, "Victim")
        found = await UserLookupService(session).resolve(query=str(user.telegram_id))
        assert found is not None and found.id == user.id

    async def test_by_at_username(self, session):
        await make_user(session, "Stalker")
        found = await UserLookupService(session).resolve(query="@Stalker")
        assert found is not None and found.username == "stalker"

    async def test_by_bare_username(self, session):
        await make_user(session, "Gambler")
        found = await UserLookupService(session).resolve(query="Gambler")
        assert found is not None and found.username == "gambler"

    async def test_username_case_insensitive(self, session):
        await make_user(session, "Nightowl")
        found = await UserLookupService(session).resolve(query="@NiGhTOwL")
        assert found is not None and found.username == "nightowl"

    async def test_by_reply_fallback(self, session):
        user = await make_user(session, "Replied")
        found = await UserLookupService(session).resolve(
            query=None, reply_telegram_id=user.telegram_id
        )
        assert found is not None and found.id == user.id

    async def test_query_wins_over_reply(self, session):
        queried = await make_user(session, "Queried")
        other = await make_user(session, "Other")
        found = await UserLookupService(session).resolve(
            query=str(queried.telegram_id), reply_telegram_id=other.telegram_id
        )
        assert found is not None and found.id == queried.id

    async def test_failed_query_falls_back_to_reply(self, session):
        user = await make_user(session, "Fallback")
        found = await UserLookupService(session).resolve(
            query="no_such_user", reply_telegram_id=user.telegram_id
        )
        assert found is not None and found.id == user.id

    async def test_unknown_returns_none(self, session):
        await make_user(session, "Someone")
        svc = UserLookupService(session)
        assert await svc.resolve(query="999999999") is None
        assert await svc.resolve(query="@ghost") is None
        assert await svc.resolve(query=None, reply_telegram_id=None) is None
