"""Награды: достижения, титулы, ивентовые награды/роли.

Достижения и титулы (определения) — в коде (achievements.py / titles.py);
факты получения/открытия — в БД. Выдача ивентовых наград — только через
админ-команды (проверка прав в хендлере, не здесь).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database.models import User
from bot.database.repositories.social import (
    EventRewardRepository,
    UserAchievementRepository,
    UserEventRewardRepository,
    UserTitleRepository,
)
from bot.database.repositories.users import UserRepository
from bot.services import achievements as ach
from bot.services import titles as ttl
from bot.utils.helpers import utcnow

logger = logging.getLogger(__name__)


# --------------------------- достижения/титулы (в рамках транзакции) ----------


async def award_achievements(
    session: AsyncSession,
    earned: dict[int, set[str]],
    wins_after: dict[int, int] | None = None,
) -> dict[int, list[ach.Achievement]]:
    """Фиксирует НОВЫЕ достижения, открывает связанные титулы.

    Возвращает {user_id: [новые Achievement]} для уведомления игрока.
    Одноразовые: повторно не выдаются (unique user+achievement).
    """
    ach_repo = UserAchievementRepository(session)
    title_repo = UserTitleRepository(session)
    newly: dict[int, list[ach.Achievement]] = {}

    for user_id, ids in earned.items():
        for aid in ids:
            definition = ach.get_achievement(aid)
            if definition is None:
                continue
            if not await ach_repo.award(user_id, aid):
                continue  # уже было
            newly.setdefault(user_id, []).append(definition)
            # открываем связанный титул (если есть)
            title_id = ttl.TITLE_UNLOCKS.get(aid)
            if title_id:
                await title_repo.unlock(user_id, title_id, source="achievement")
    return newly


async def grant_title(
    session: AsyncSession, user_id: int, title_id: str, source: str = "admin"
) -> bool:
    if ttl.get_title(title_id) is None:
        return False
    return await UserTitleRepository(session).unlock(user_id, title_id, source=source)


async def set_active_title(session: AsyncSession, user_id: int, title_id: str | None) -> bool:
    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return False
    if title_id is not None:
        unlocked = await UserTitleRepository(session).ids_of(user_id)
        if title_id not in unlocked:
            return False
    user.active_title = title_id
    await session.flush()
    return True


async def unlocked_titles(session: AsyncSession, user_id: int) -> list[ttl.Title]:
    ids = await UserTitleRepository(session).ids_of(user_id)
    out = [ttl.get_title(i) for i in ids]
    return [t for t in out if t is not None]


# ------------------------------- ивентовые награды (хендлеры) -----------------


class RewardService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def list_catalog(self) -> list:
        async with self.session_factory() as session:
            return await EventRewardRepository(session).all()

    async def create_reward(
        self, code: str, name: str, emoji: str, description: str,
        kind: str, expires_days: int | None, created_by: int,
    ) -> tuple[bool, str]:
        code = code.strip().lower()
        if not code or not name:
            return False, "Укажи code и name."
        async with self.session_factory() as session:
            repo = EventRewardRepository(session)
            if await repo.get_by_code(code) is not None:
                return False, f"Награда с кодом «{code}» уже существует."
            await repo.create(code, name, emoji, description, kind, expires_days, created_by)
            await session.commit()
        return True, f"✅ Ивентовая награда «{name}» ({code}) создана."

    async def grant(
        self, target_user_id: int, code: str, awarded_by: int, expires_days: int | None = None,
    ) -> tuple[bool, str]:
        async with self.session_factory() as session:
            reward = await EventRewardRepository(session).get_by_code(code.strip().lower())
            if reward is None:
                return False, f"Ивентовая награда «{code}» не найдена."
            days = expires_days if expires_days is not None else reward.expires_days
            expires_at = utcnow() + timedelta(days=days) if days else None
            row = await UserEventRewardRepository(session).grant(
                target_user_id, reward.id, awarded_by, expires_at=expires_at
            )
            # новая выдача сразу становится активной
            user = await UserRepository(session).get_by_id(target_user_id)
            if user is not None:
                user.active_event_reward_id = row.id
            await session.commit()
        return True, f"🎪 Награда «{reward.name}» выдана."

    async def user_rewards(self, user_id: int) -> list:
        async with self.session_factory() as session:
            return await UserEventRewardRepository(session).of_user(user_id)

    async def set_active(self, user_id: int, user_reward_id: int) -> tuple[bool, str]:
        async with self.session_factory() as session:
            rows = await UserEventRewardRepository(session).of_user(user_id)
            row = next((r for r in rows if r.id == user_reward_id), None)
            if row is None:
                return False, "Эта награда тебе не принадлежит."
            if row.expires_at is not None and row.expires_at <= utcnow():
                return False, "Срок действия этой награды истёк."
            user = await UserRepository(session).get_by_id(user_id)
            if user is None:
                return False, "Пользователь не найден."
            user.active_event_reward_id = row.id
            await session.commit()
        return True, "✅ Активная награда обновлена."

    async def active_display(self, session: AsyncSession, user: User) -> str:
        """Готовая строка активной (не истекшей) ивентовой награды для профиля."""
        if not user.active_event_reward_id:
            return ""
        from bot.database.repositories.social import EventRewardRepository

        row = await UserEventRewardRepository(session).get(user.active_event_reward_id)
        if row is None or (row.expires_at is not None and row.expires_at <= utcnow()):
            return ""
        reward = await EventRewardRepository(session).get(row.reward_id)
        if reward is None:
            return ""
        return f"{reward.emoji} {reward.name}"
