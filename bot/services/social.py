"""SocialService: друзья, запросы в друзья, игнор-лист, избранное.

Бизнес-правила (валидации, защита от дублей/само-добавления) живут здесь;
репозитории — тонкая обёртка над БД. Поиск цели переиспользует существующий
UserLookupService (Telegram ID / @username / reply).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import User
from bot.database.repositories.social import (
    FavoriteRepository,
    FriendshipRepository,
    FriendRequestRepository,
    UserBlockRepository,
)
from bot.database.repositories.users import UserRepository

logger = logging.getLogger(__name__)


class SocialService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    # -------------------------------------------------------------- друзья

    async def send_request(self, from_id: int, to_id: int) -> tuple[bool, str]:
        if from_id == to_id:
            return False, "Нельзя добавить в друзья самого себя."
        async with self.session_factory() as session:
            users = UserRepository(session)
            target = await users.get_by_id(to_id)
            if target is None:
                return False, "Пользователь не найден."
            if await FriendshipRepository(session).are_friends(from_id, to_id):
                return False, "Вы уже друзья."
            req_repo = FriendRequestRepository(session)
            # обратный запрос уже есть — сразу принимаем
            reverse = await req_repo.get(to_id, from_id)
            if reverse is not None:
                await req_repo.remove(to_id, from_id)
                await self._become_friends(session, from_id, to_id)
                await session.commit()
                return True, "✅ Вы теперь друзья!"
            # прямой дубль
            existing = await req_repo.get(from_id, to_id)
            if existing is not None:
                return False, "Запрос в друзья уже отправлен — ждём ответа."
            await req_repo.add(from_id, to_id)
            await session.commit()
        return True, "📨 Запрос в друзья отправлен."

    async def _become_friends(self, session, a: int, b: int) -> None:
        fr = FriendshipRepository(session)
        await fr.add(a, b)
        await fr.add(b, a)

    async def accept_request(self, to_id: int, from_id: int) -> tuple[bool, str]:
        if from_id == to_id:
            return False, "Нельзя принять запрос от себя."
        async with self.session_factory() as session:
            req_repo = FriendRequestRepository(session)
            req = await req_repo.get(from_id, to_id)
            if req is None:
                return False, "Такого запроса в друзья нет."
            await req_repo.remove(from_id, to_id)
            await self._become_friends(session, from_id, to_id)
            await session.commit()
        return True, "✅ Вы теперь друзья!"

    async def decline_request(self, to_id: int, from_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            req_repo = FriendRequestRepository(session)
            removed = await req_repo.remove(from_id, to_id)
            await session.commit()
        return (removed, "🚫 Запрос отклонён.") if removed else (False, "Такого запроса нет.")

    async def remove_friend(self, user_id: int, friend_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            removed = await FriendshipRepository(session).remove(user_id, friend_id)
            await session.commit()
        return (removed, "👋 Удалён из друзей.") if removed else (False, "Этого игрока нет в друзьях.")

    async def pending_requests(self, user_id: int) -> list[User]:
        async with self.session_factory() as session:
            reqs = await FriendRequestRepository(session).pending_to(user_id)
            users = UserRepository(session)
            result = []
            for req in reqs:
                u = await users.get_by_id(req.from_user_id)
                if u is not None:
                    result.append(u)
            return result

    async def friends_of(self, user_id: int) -> list[User]:
        async with self.session_factory() as session:
            ids = await FriendshipRepository(session).list_friends(user_id)
            users = UserRepository(session)
            out = []
            for fid in ids:
                u = await users.get_by_id(fid)
                if u is not None:
                    out.append(u)
            return out

    async def are_friends(self, user_id: int, other_id: int) -> bool:
        async with self.session_factory() as session:
            return await FriendshipRepository(session).are_friends(user_id, other_id)

    # --------------------------------------------------------------- игнор

    async def block(self, user_id: int, blocked_id: int) -> tuple[bool, str]:
        if user_id == blocked_id:
            return False, "Нельзя игнорировать самого себя."
        async with self.session_factory() as session:
            target = await UserRepository(session).get_by_id(blocked_id)
            if target is None:
                return False, "Пользователь не найден."
            added = await UserBlockRepository(session).add(user_id, blocked_id)
            await session.commit()
        return (added, "🚫 Игрок добавлен в игнор-лист.") if added else (False, "Игрок уже в игнор-листе.")

    async def unblock(self, user_id: int, blocked_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            removed = await UserBlockRepository(session).remove(user_id, blocked_id)
            await session.commit()
        return (removed, "✅ Игрок удалён из игнор-листа.") if removed else (False, "Этого игрока нет в игнор-листе.")

    async def blocked_users(self, user_id: int) -> list[User]:
        async with self.session_factory() as session:
            ids = await UserBlockRepository(session).blocked_ids(user_id)
            users = UserRepository(session)
            out = []
            for bid in ids:
                u = await users.get_by_id(bid)
                if u is not None:
                    out.append(u)
            return out

    async def is_blocked(self, user_id: int, other_id: int) -> bool:
        async with self.session_factory() as session:
            return await UserBlockRepository(session).is_blocked(user_id, other_id)

    # --------------------------------------------------------------- избранное

    async def favorite(self, user_id: int, favorite_id: int) -> tuple[bool, str]:
        if user_id == favorite_id:
            return False, "Нельзя добавить в избранное самого себя."
        async with self.session_factory() as session:
            target = await UserRepository(session).get_by_id(favorite_id)
            if target is None:
                return False, "Пользователь не найден."
            added = await FavoriteRepository(session).add(user_id, favorite_id)
            await session.commit()
        return (added, "⭐ Добавлен в избранное.") if added else (False, "Игрок уже в избранном.")

    async def unfavorite(self, user_id: int, favorite_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            removed = await FavoriteRepository(session).remove(user_id, favorite_id)
            await session.commit()
        return (removed, "✅ Удалён из избранного.") if removed else (False, "Этого игрока нет в избранном.")

    async def favorites_of(self, user_id: int) -> list[User]:
        async with self.session_factory() as session:
            ids = await FavoriteRepository(session).list_ids(user_id)
            users = UserRepository(session)
            out = []
            for fid in ids:
                u = await users.get_by_id(fid)
                if u is not None:
                    out.append(u)
            return out
