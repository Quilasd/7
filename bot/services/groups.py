"""GroupService: группы, локальные настройки,_staff, локальные топы, комнаты групп.

Все операции с группой принимают group_id — права проверяются относительно
конкретного чата вызывающим кодом (PermissionService), здесь — бизнес-логика.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Group, GroupAdmin, GroupPlayer, GroupSettingsModel, Room, RoomPlayer, RoomStatus
from bot.database.repositories.groups import (
    GroupAdminRepository,
    GroupPlayerRepository,
    GroupRepository,
    GroupSettingsRepository,
)
from bot.database.repositories.rooms import RoomRepository
from bot.services.permissions import AdminLevel, PermissionService
from bot.utils.helpers import utcnow

logger = logging.getLogger(__name__)


async def effective_ban(session, group_id: int, user_id: int) -> tuple[bool, object | None]:
    """(забанен ли сейчас, GroupPlayer). Лениво снимает истёкший временный бан.

    Свободная функция — переиспользуется RoomService без создания GroupService.
    """
    from bot.database.repositories.groups import GroupPlayerRepository

    gp = await GroupPlayerRepository(session).get_membership(group_id, user_id)
    if gp is None:
        return False, None
    if gp.is_banned and gp.banned_until is not None and gp.banned_until <= utcnow():
        gp.is_banned = False
        gp.banned_until = None
        await session.commit()
        return False, gp
    return gp.is_banned, gp


class GroupService:
    def __init__(self, session_factory: async_sessionmaker, permissions: PermissionService) -> None:
        self.session_factory = session_factory
        self.permissions = permissions

    # ------------------------------------------------------------- группа

    async def get_or_create(self, telegram_chat_id: int, title: str = "") -> Group:
        async with self.session_factory() as session:
            group = await GroupRepository(session).get_or_create(telegram_chat_id, title)
            # первой настройкой и записью создаём сразу
            await GroupSettingsRepository(session).get_or_create(group.id)
            await session.commit()
            return group

    async def get_by_chat_id(self, telegram_chat_id: int) -> Group | None:
        async with self.session_factory() as session:
            return await GroupRepository(session).get_by_chat_id(telegram_chat_id)

    async def ensure_player(self, group_id: int, user_id: int) -> GroupPlayer:
        """Регистрирует участника группы (нулевая локальная статистика)."""
        async with self.session_factory() as session:
            gp = await GroupPlayerRepository(session).ensure(group_id, user_id)
            await session.commit()
            return gp

    # ------------------------------------------------------------ настройки

    async def get_settings(self, group_id: int) -> GroupSettingsModel:
        async with self.session_factory() as session:
            return await GroupSettingsRepository(session).get_or_create(group_id)

    async def update_settings(self, group_id: int, mutate) -> GroupSettingsModel:
        """mutate(GroupSettingsModel) -> None; изменения коммитятся."""
        async with self.session_factory() as session:
            repo = GroupSettingsRepository(session)
            settings = await repo.get_or_create(group_id)
            mutate(settings)
            await session.commit()
            return settings

    # ----------------------------------------------------------- админы

    async def list_staff(self, group_id: int) -> list[GroupAdmin]:
        async with self.session_factory() as session:
            return await GroupAdminRepository(session).list_for_group(group_id)

    async def get_staff_member(self, group_id: int, user_id: int) -> GroupAdmin | None:
        async with self.session_factory() as session:
            return await GroupAdminRepository(session).get_for(group_id, user_id)

    async def set_staff(
        self,
        group_id: int,
        actor_telegram_id: int,
        actor_level: AdminLevel,
        target_user_id: int,
        new_level: int,
        actor_user_id: int,
    ) -> tuple[bool, str]:
        """Назначение/изменение уровня с защитой иерархии (§21) + аудит."""
        from dataclasses import dataclass

        from bot.database.repositories.groups import AuditLogRepository

        @dataclass
        class _Actor:
            level: AdminLevel

        async with self.session_factory() as session:
            admins = GroupAdminRepository(session)
            current = AdminLevel(await admins.level_of(group_id, target_user_id))
            allowed, reason = self.permissions.can_manage_staff_level(
                actor=_Actor(actor_level),
                target_current=current,
                new_level=AdminLevel(new_level),
                same_user=False,
            )
            if not allowed:
                return False, reason
            await admins.set_level(group_id, target_user_id, new_level, actor_user_id)
            await AuditLogRepository(session).log(
                actor_id=actor_user_id,
                target_id=target_user_id,
                group_id=group_id,
                action="staff_set_level",
                details=f"{current.value} -> {new_level}",
            )
            await session.commit()
        logger.info("Группа %s: уровень %s для user=%s (актёр %s)", group_id, new_level, target_user_id, actor_user_id)
        return True, "Уровень обновлён."

    async def remove_staff(
        self, group_id: int, actor_level: AdminLevel, target_user_id: int, actor_user_id: int
    ) -> tuple[bool, str]:
        from dataclasses import dataclass

        from bot.database.repositories.groups import AuditLogRepository

        @dataclass
        class _Actor:
            level: AdminLevel

        async with self.session_factory() as session:
            admins = GroupAdminRepository(session)
            current = AdminLevel(await admins.level_of(group_id, target_user_id))
            allowed, reason = self.permissions.can_manage_staff_level(
                actor=_Actor(actor_level),
                target_current=current,
                new_level=AdminLevel.PLAYER,
                same_user=False,
            )
            if not allowed:
                return False, reason
            removed = await admins.remove(group_id, target_user_id)
            if not removed:
                return False, "Этот игрок не администратор."
            await AuditLogRepository(session).log(
                actor_id=actor_user_id,
                target_id=target_user_id,
                group_id=group_id,
                action="staff_remove",
                details=f"level was {current.value}",
            )
            await session.commit()
        return True, "Администратор снят."

    async def claim_creator(self, group_id: int, user_id: int) -> tuple[bool, str]:
        """Выдаёт Senior Admin (4) реальному создателю группы по /claim.

        Хендлер уже проверил, что в Telegram вызывающий — создатель чата; здесь
        остаётся только бизнес-логика уровня. Если уровень уже >= Senior Admin —
        no-op. Действие пишется в аудит (actor == target == user).
        """
        from bot.database.repositories.groups import AuditLogRepository

        async with self.session_factory() as session:
            admins = GroupAdminRepository(session)
            current = AdminLevel(await admins.level_of(group_id, user_id))
            if current >= AdminLevel.SENIOR_ADMIN:
                return False, "уже есть права"
            await admins.set_level(
                group_id, user_id, AdminLevel.SENIOR_ADMIN, created_by=0
            )
            await AuditLogRepository(session).log(
                actor_id=user_id,
                target_id=user_id,
                group_id=group_id,
                action="group_claim",
                details=f"creator -> senior (was {current.value})",
            )
            await session.commit()
        logger.info(
            "Группа %s: %s забрал права создателя (был уровень %s)",
            group_id, user_id, current.value,
        )
        return True, (
            "👑 Ты создатель группы — выдан уровень 🎖 Senior Admin! "
            "Управляй: /settings · /staff_add · /createroom · /top"
        )

    # -------------------------------------------------------- топы группы

    async def local_top(
        self, group_id: int, metric: str = "rating", limit: int = 10, offset: int = 0
    ) -> list[GroupPlayer]:
        async with self.session_factory() as session:
            return await GroupPlayerRepository(session).top(group_id, metric, limit, offset)

    async def local_player(self, group_id: int, user_id: int) -> GroupPlayer | None:
        async with self.session_factory() as session:
            return await GroupPlayerRepository(session).get_membership(group_id, user_id)

    # -------------------------------------------------------- варны 2.0

    async def warn(
        self,
        group_id: int,
        target_user_id: int,
        actor_user_id: int,
        reason: str = "",
        duration_hours: int | None = None,
    ) -> dict:
        """Выдаёт варн с причиной и сроком действия.

        Возвращает {count, limit, auto_ban_until, warn}. При count == limit
        срабатывает авто-бан на GroupSettings.warn_ban_minutes, активные варны
        израсходованы (revoked) и счётчик обнуляется.
        """
        from datetime import timedelta

        from bot.database.models import GroupWarning
        from bot.database.repositories.groups import AuditLogRepository, GroupSettingsRepository

        async with self.session_factory() as session:
            settings = await GroupSettingsRepository(session).get_for(group_id)
            expire_hours = duration_hours if duration_hours is not None else (
                settings.warn_expire_hours if settings else 168
            )
            limit = settings.warn_limit if settings else 3
            ban_minutes = settings.warn_ban_minutes if settings else 1440

            warn = GroupWarning(
                group_id=group_id,
                user_id=target_user_id,
                actor_id=actor_user_id,
                reason=(reason or "")[:500],
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(hours=max(1, expire_hours)),
            )
            session.add(warn)

            repo = GroupPlayerRepository(session)
            gp = await repo.ensure(group_id, target_user_id)
            # считаем АКТИВНЫЕ до вставки нового: ensure() делает flush,
            # поэтому помечаем сессию без автофлаша на время подсчёта
            import sqlalchemy as sa

            result = await session.execute(
                sa.select(GroupWarning.id)
                .where(
                    GroupWarning.group_id == group_id,
                    GroupWarning.user_id == target_user_id,
                    GroupWarning.revoked.is_(False),
                    GroupWarning.expires_at > utcnow(),
                    GroupWarning.id != warn.id,
                )
            )
            count = len(result.all()) + 1

            auto_ban_until = None
            if count >= limit:
                # 3/3: авто-бан на время, варны израсходованы
                auto_ban_until = utcnow() + timedelta(minutes=ban_minutes)
                gp.is_banned = True
                gp.banned_until = auto_ban_until
                from sqlalchemy import update as sa_update

                await session.execute(
                    sa_update(GroupWarning)
                    .where(
                        GroupWarning.group_id == group_id,
                        GroupWarning.user_id == target_user_id,
                        GroupWarning.revoked.is_(False),
                    )
                    .values(revoked=True)
                )
                count = 0

            gp.warnings = count
            await AuditLogRepository(session).log(
                actor_id=actor_user_id, target_id=target_user_id, group_id=group_id,
                action="warn", details=f"reason={warn.reason[:120]!r} count={count if not auto_ban_until else 'limit'}",
            )
            await session.commit()
            return {
                "count": count,
                "limit": limit,
                "auto_ban_until": auto_ban_until,
                "ban_minutes": ban_minutes,
                "warn": warn,
            }

    async def unwarn(self, group_id: int, target_user_id: int, actor_user_id: int) -> int:
        """Снимает последний активный варн. Возвращает остаток активных."""
        from bot.database.models import GroupWarning
        from bot.database.repositories.groups import AuditLogRepository

        async with self.session_factory() as session:
            active = await self._active_warnings_of(session, group_id, target_user_id)
            if active:
                active[-1].revoked = True
            count = len(active) - 1 if active else 0
            repo = GroupPlayerRepository(session)
            gp = await repo.ensure(group_id, target_user_id)
            gp.warnings = max(0, count)
            await AuditLogRepository(session).log(
                actor_id=actor_user_id, target_id=target_user_id, group_id=group_id,
                action="unwarn", details=f"warnings={gp.warnings}",
            )
            await session.commit()
            return gp.warnings

    async def _active_warnings_of(self, session, group_id: int, user_id: int) -> list:
        """Активные (не отозванные, не истёкшие) варны, старые -> новые."""
        from bot.database.models import GroupWarning

        result = await session.execute(
            select(GroupWarning)
            .where(
                GroupWarning.group_id == group_id,
                GroupWarning.user_id == user_id,
                GroupWarning.revoked.is_(False),
                GroupWarning.expires_at > utcnow(),
            )
            .order_by(GroupWarning.created_at)
        )
        return list(result.scalars().unique().all())

    async def _active_warns(self, session, group_id: int, user_id: int) -> int:
        return len(await self._active_warnings_of(session, group_id, user_id))

    async def warnings_of(self, group_id: int, user_id: int) -> list:
        """Активные варны игрока (для /warnings)."""
        async with self.session_factory() as session:
            return await self._active_warnings_of(session, group_id, user_id)

    async def effective_ban(
        self, session, group_id: int, user_id: int
    ) -> tuple[bool, object | None]:
        """(забанен ли сейчас, GroupPlayer). Лениво снимает истёкший временный бан."""
        return await effective_ban(session, group_id, user_id)

    async def set_local_ban(
        self,
        group_id: int,
        target_user_id: int,
        banned: bool,
        actor_user_id: int,
        until=None,
    ) -> tuple[bool, str]:
        """Локальный бан группы. until=None — навсегда, иначе временный."""
        from bot.database.repositories.groups import AuditLogRepository

        async with self.session_factory() as session:
            repo = GroupPlayerRepository(session)
            gp = await repo.ensure(group_id, target_user_id)
            if gp.is_banned == banned:
                return banned, "Уже в таком состоянии."
            gp.is_banned = banned
            gp.banned_until = until if banned else None
            await AuditLogRepository(session).log(
                actor_id=actor_user_id, target_id=target_user_id, group_id=group_id,
                action="local_ban" if banned else "local_unban",
                details=f"until={until.isoformat() if until else 'perm'}",
            )
            await session.commit()
        return banned, "Локальный бан установлен." if banned else "Локальный бан снят."

    # ---------------------------------------------------- комната группы

    async def create_room_in_group(
        self, group_id: int, creator_user_id: int, name: str | None = None
    ) -> tuple[Room | None, str]:
        """Комната с правилами группы (таймеры, роли, лимиты) и привязкой к ней."""
        from bot.services.rooms import RoomSettings
        from bot.services.role_manager import validate_setup

        # забаненный в группе не может создавать в ней комнаты
        async with self.session_factory() as session:
            banned, gp = await self.effective_ban(session, group_id, creator_user_id)
        if banned:
            until = f" до {gp.banned_until:%d.%m.%Y %H:%M}" if gp and gp.banned_until else ""
            return None, f"🚫 Ты забанен в этой группе{until}."

        settings = await self.get_settings(group_id)

        roles: dict[str, int] = {"mafia": max(1, settings.mafia_count), "detective": 1, "doctor": 1}
        if settings.allow_maniac and settings.max_players >= 6:
            roles["maniac"] = 1
        if settings.enabled_roles:
            # фильтр по ролям, разрешённым настройками группы
            allowed = set(settings.enabled_roles)
            roles = {rid: cnt for rid, cnt in roles.items() if rid in allowed}
            roles.setdefault("mafia", 1)

        room_settings = RoomSettings(
            roles=roles,
            night_seconds=settings.night_seconds,
            day_seconds=settings.discussion_seconds or settings.day_seconds,
            vote_seconds=settings.vote_seconds,
            tie_rule=settings.tie_rule if settings.tie_rule in ("revote", "no_death") else "revote",
            reveal_roles_on_death=settings.role_reveal_on_death,
        )
        errors = validate_setup(room_settings.roles, settings.max_players, settings.min_players)
        if errors:
            return None, "Настройки группы некорректны: " + "; ".join(errors)

        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            existing = await rooms.open_room_of_user(creator_user_id)
            if existing:
                return None, f"Ты уже в комнате #{existing.id}. Сначала покинь её."
            group = await GroupRepository(session).get(group_id)
            title = group.title if group and group.title else f"Группа {group_id}"
            room = Room(
                creator_id=creator_user_id,
                name=(name or f"🏠 {title}")[:64],
                max_players=settings.max_players,
                min_players=settings.min_players,
                is_private=False,
                status=RoomStatus.OPEN.value,
                settings=room_settings.model_dump(mode="json"),
                group_id=group_id,
            )
            session.add(room)
            await session.flush()
            session.add(RoomPlayer(room_id=room.id, user_id=creator_user_id, is_ready=False))
            await session.commit()
            room_id = room.id
        logger.info("Группа %s: создана комната %s", group_id, room_id)
        async with self.session_factory() as session:
            return await RoomRepository(session).get(room_id), "Комната группы создана."
