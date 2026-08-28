"""SocialService: друзья, запросы в друзья, игнор-лист, избранное.

Бизнес-правила (валидации, защита от дублей/само-добавления) живут здесь;
репозитории — тонкая обёртка над БД. Поиск цели переиспользует существующий
UserLookupService (Telegram ID / @username / reply).

Правила избранного: в избранное можно добавить ТОЛЬКО друга (проверка здесь,
на уровне сервиса — хендлеры/колбэки её обойти не могут); при unfriend
избранное снимается в обе стороны, поэтому «призрачных» избранных не бывает.

Отношения (друзья/избранное/игнор) — ГЛОБАЛЬНЫЕ, на уровне пользователей
бота: в таблицах нет group_id, команды работают одинаково в ЛС и группах.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
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
            if target.is_test:
                return False, "Это тестовый игрок — его нельзя добавить в друзья."
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
            try:
                await session.commit()
            except IntegrityError:  # гонка: параллельный такой же запрос
                await session.rollback()
                return False, "Запрос в друзья уже отправлен — ждём ответа."
        return True, "📨 Запрос в друзья отправлен."

    async def _become_friends(self, session, a: int, b: int) -> None:
        """Идемпотентно: повторный вызов (двойной accept, гонка) не падает."""
        fr = FriendshipRepository(session)
        if not await fr.are_friends(a, b):
            await fr.add(a, b)
        if not await fr.are_friends(b, a):
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
            if removed:
                # избранное существует только между друзьями — снимаем в обе
                # стороны, чтобы не оставалось «призрачных» избранных
                favs = FavoriteRepository(session)
                await favs.remove(user_id, friend_id)
                await favs.remove(friend_id, user_id)
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
            try:
                await session.commit()
            except IntegrityError:  # гонка: параллельный /ignore того же игрока
                await session.rollback()
                return False, "Игрок уже в игнор-листе."
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
            # избранное — только для друзей; проверка в сервисе, чтобы её
            # нельзя было обойти другим хендлером или прямым вызовом репозитория
            if not await FriendshipRepository(session).are_friends(user_id, favorite_id):
                return False, "❌ Нельзя добавить в избранное: пользователь не является вашим другом."
            added = await FavoriteRepository(session).add(user_id, favorite_id)
            try:
                await session.commit()
            except IntegrityError:  # гонка: параллельное /favorite того же игрока
                await session.rollback()
                return False, "Игрок уже в избранном."
        return (added, "⭐ Добавлен в избранное.") if added else (False, "Игрок уже в избранном.")

    async def unfavorite(self, user_id: int, favorite_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            removed = await FavoriteRepository(session).remove(user_id, favorite_id)
            await session.commit()
        return (removed, "✅ Удалён из избранного.") if removed else (False, "Этого игрока нет в избранном.")

    async def favorites_of(self, user_id: int) -> list[User]:
        async with self.session_factory() as session:
            # показываем только действующих друзей — пережитки (строки,
            # оставшиеся до введения правила «избранное только для друзей»)
            # в списке не появляются
            friends = set(await FriendshipRepository(session).list_friends(user_id))
            ids = await FavoriteRepository(session).list_ids(user_id)
            users = UserRepository(session)
            out = []
            for fid in ids:
                if fid not in friends:
                    continue
                u = await users.get_by_id(fid)
                if u is not None:
                    out.append(u)
            return out
