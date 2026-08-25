"""Репозитории групп: Group, GroupPlayer, GroupAdmin, GroupSettings, AuditLog.

Глобальная (User) и локальная (GroupPlayer) статистика физически разделены —
эти репозитории работают ТОЛЬКО с локальной.
"""

from __future__ import annotations

from sqlalchemy import func, select

from bot.database.models import AuditLog, Group, GroupAdmin, GroupPlayer, GroupSettingsModel
from bot.database.repositories.base import BaseRepository

# Поля локальной статистики (идентичны глобальным в User)
LOCAL_STAT_FIELDS = (
    "games_played", "wins", "losses", "kills", "saves",
    "investigations", "correct_votes", "rating", "xp", "level",
)


class GroupRepository(BaseRepository[Group]):
    model = Group

    async def get_by_chat_id(self, telegram_chat_id: int) -> Group | None:
        result = await self.session.execute(
            select(Group).where(Group.telegram_chat_id == telegram_chat_id)
        )
        return result.scalars().unique().one_or_none()

    async def get_or_create(self, telegram_chat_id: int, title: str = "") -> Group:
        group = await self.get_by_chat_id(telegram_chat_id)
        if group is None:
            group = Group(telegram_chat_id=telegram_chat_id, title=title[:128])
            self.session.add(group)
            await self.session.flush()
        elif title and group.title != title:
            group.title = title[:128]
            await self.session.flush()
        return group

    async def count_all(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(Group))
        return int(result.scalar_one())


class GroupPlayerRepository(BaseRepository[GroupPlayer]):
    model = GroupPlayer

    async def get(self, group_player_id: int) -> GroupPlayer | None:
        return await self.session.get(GroupPlayer, group_player_id)

    async def get_membership(self, group_id: int, user_id: int) -> GroupPlayer | None:
        result = await self.session.execute(
            select(GroupPlayer).where(
                GroupPlayer.group_id == group_id, GroupPlayer.user_id == user_id
            )
        )
        return result.scalars().unique().one_or_none()

    async def ensure(self, group_id: int, user_id: int) -> GroupPlayer:
        """Создаёт запись участника с нулевой локальной статистикой при первом входе."""
        gp = await self.get_membership(group_id, user_id)
        if gp is None:
            gp = GroupPlayer(group_id=group_id, user_id=user_id)
            self.session.add(gp)
            await self.session.flush()
        return gp

    async def list_for_group(self, group_id: int, limit: int = 100) -> list[GroupPlayer]:
        result = await self.session.execute(
            select(GroupPlayer)
            .where(GroupPlayer.group_id == group_id)
            .order_by(GroupPlayer.joined_at)
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    _METRIC_COLUMNS = {
        "rating": GroupPlayer.rating,
        "wins": GroupPlayer.wins,
        "level": GroupPlayer.level,
        "xp": GroupPlayer.xp,
    }

    async def top(
        self, group_id: int, metric: str = "rating", limit: int = 10, offset: int = 0
    ) -> list[GroupPlayer]:
        """Топ группы по метрике: rating|wins|level|xp."""
        column = self._METRIC_COLUMNS.get(metric, GroupPlayer.rating)
        secondary = GroupPlayer.xp if metric == "level" else column
        result = await self.session.execute(
            select(GroupPlayer)
            .where(GroupPlayer.group_id == group_id, GroupPlayer.is_banned.is_(False))
            .order_by(column.desc(), secondary.desc(), GroupPlayer.user_id)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def count_active(self, group_id: int) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(GroupPlayer)
            .where(GroupPlayer.group_id == group_id, GroupPlayer.is_banned.is_(False))
        )
        return int(result.scalar_one())

    async def groups_of_user(self, user_id: int) -> list[GroupPlayer]:
        result = await self.session.execute(
            select(GroupPlayer).where(GroupPlayer.user_id == user_id).order_by(GroupPlayer.joined_at)
        )
        return list(result.scalars().unique().all())

    async def rank_in_group(self, group_id: int, metric: str, value: int, secondary: int = 0) -> int:
        """Позиция в локальном топе группы по метрике (rating|wins|level).

        secondary — XP для тай-брейка при metric='level'.
        """
        col = self._METRIC_COLUMNS.get(metric, GroupPlayer.rating)
        if metric == "level":
            cond = (col > value) | ((col == value) & (GroupPlayer.xp > secondary))
        else:
            cond = col > value
        result = await self.session.execute(
            select(func.count()).select_from(GroupPlayer).where(
                GroupPlayer.group_id == group_id, GroupPlayer.is_banned.is_(False), cond
            )
        )
        return int(result.scalar_one()) + 1


class GroupAdminRepository(BaseRepository[GroupAdmin]):
    model = GroupAdmin

    async def get(self, group_admin_id: int) -> GroupAdmin | None:
        return await self.session.get(GroupAdmin, group_admin_id)

    async def get_for(self, group_id: int, user_id: int) -> GroupAdmin | None:
        result = await self.session.execute(
            select(GroupAdmin).where(
                GroupAdmin.group_id == group_id, GroupAdmin.user_id == user_id
            )
        )
        return result.scalars().unique().one_or_none()

    async def level_of(self, group_id: int, user_id: int) -> int:
        row = await self.get_for(group_id, user_id)
        return int(row.admin_level) if row else 0

    async def list_for_group(self, group_id: int) -> list[GroupAdmin]:
        result = await self.session.execute(
            select(GroupAdmin)
            .where(GroupAdmin.group_id == group_id)
            .order_by(GroupAdmin.admin_level.desc())
        )
        return list(result.scalars().unique().all())

    async def set_level(self, group_id: int, user_id: int, level: int, created_by: int) -> GroupAdmin:
        row = await self.get_for(group_id, user_id)
        if row is None:
            row = GroupAdmin(
                group_id=group_id, user_id=user_id, admin_level=level, created_by=created_by
            )
            self.session.add(row)
        else:
            row.admin_level = level
        await self.session.flush()
        return row

    async def remove(self, group_id: int, user_id: int) -> bool:
        row = await self.get_for(group_id, user_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True


class GroupSettingsRepository(BaseRepository[GroupSettingsModel]):
    model = GroupSettingsModel

    async def get_for(self, group_id: int) -> GroupSettingsModel | None:
        return await self.session.get(GroupSettingsModel, group_id)

    async def get_or_create(self, group_id: int) -> GroupSettingsModel:
        settings = await self.get_for(group_id)
        if settings is None:
            settings = GroupSettingsModel(group_id=group_id)
            self.session.add(settings)
            await self.session.flush()
        return settings


class AuditLogRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def log(
        self,
        actor_id: int,
        action: str,
        target_id: int | None = None,
        group_id: int | None = None,
        details: str = "",
    ) -> AuditLog:
        entry = AuditLog(
            actor_id=actor_id,
            target_id=target_id,
            group_id=group_id,
            action=action[:48],
            details=details[:512],
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def last(self, group_id: int | None = None, limit: int = 20) -> list[AuditLog]:
        query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if group_id is not None:
            query = query.where(AuditLog.group_id == group_id)
        result = await self.session.execute(query)
        return list(result.scalars().unique().all())
