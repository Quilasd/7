"""PermissionService: многоуровневая администрация.

Уровни:
    0 Player | 1 Helper | 2 Moderator | 3 Admin | 4 Senior Admin | 5 Developer/Owner

Источники прав:
- ГЛОБАЛЬНО: OWNER_IDS -> уровень 5 везде (глобальный Owner),
  ADMIN_IDS -> уровень 4 (глобальный senior admin);
- ЛОКАЛЬНО: GroupAdmin.admin_level в конкретной группе.

Эффективный уровень = max(глобальный, локальный). Права группы A
не действуют в группе B: локальный уровень берётся только по текущему chat.

Защита иерархии (staff_add/promote/demove/remove) — в GroupService.set_staff,
который использует can_manage_staff() ниже.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.database.repositories.groups import GroupAdminRepository


class AdminLevel(IntEnum):
    PLAYER = 0
    HELPER = 1
    MODERATOR = 2
    ADMIN = 3
    SENIOR_ADMIN = 4
    OWNER = 5


LEVEL_TITLES = {
    AdminLevel.PLAYER: "👤 Player",
    AdminLevel.HELPER: "🛟 Helper",
    AdminLevel.MODERATOR: "🔨 Moderator",
    AdminLevel.ADMIN: "⚙️ Admin",
    AdminLevel.SENIOR_ADMIN: "🎖 Senior Admin",
    AdminLevel.OWNER: "👑 Owner",
}


class Permission(str, Enum):
    VIEW_PROFILE = "VIEW_PROFILE"
    VIEW_PLAYERS = "VIEW_PLAYERS"
    VIEW_STATS = "VIEW_STATS"
    WARN_PLAYER = "WARN_PLAYER"
    MUTE_PLAYER = "MUTE_PLAYER"
    KICK_PLAYER = "KICK_PLAYER"
    BAN_PLAYER = "BAN_PLAYER"          # постоянный бан (Admin+)
    TEMP_BAN_PLAYER = "TEMP_BAN_PLAYER"  # временный бан со сроком (Moderator+)
    MANAGE_ROOMS = "MANAGE_ROOMS"
    START_GAME = "START_GAME"
    STOP_GAME = "STOP_GAME"
    MANAGE_SETTINGS = "MANAGE_SETTINGS"
    MANAGE_ROLES = "MANAGE_ROLES"
    BROADCAST = "BROADCAST"
    USE_DEBUG = "USE_DEBUG"
    MANAGE_STAFF = "MANAGE_STAFF"
    MANAGE_GROUP = "MANAGE_GROUP"
    MANAGE_GLOBAL_SETTINGS = "MANAGE_GLOBAL_SETTINGS"


LEVEL_PERMISSIONS: dict[AdminLevel, set[Permission]] = {
    AdminLevel.PLAYER: set(),
    # Helper — только обратимые меры: мут (до 1440 мин)
    AdminLevel.HELPER: {
        Permission.VIEW_PROFILE, Permission.VIEW_PLAYERS, Permission.VIEW_STATS,
        Permission.MUTE_PLAYER,
    },
    # Moderator — + варны (с причиной/сроком, 3/3 -> авто-бан), кик, временный бан
    AdminLevel.MODERATOR: {
        Permission.VIEW_PROFILE, Permission.VIEW_PLAYERS, Permission.VIEW_STATS,
        Permission.MUTE_PLAYER, Permission.WARN_PLAYER, Permission.KICK_PLAYER,
        Permission.TEMP_BAN_PLAYER,
    },
    # Admin — + постоянный бан/разбан
    AdminLevel.ADMIN: {
        Permission.VIEW_PROFILE, Permission.VIEW_PLAYERS, Permission.VIEW_STATS,
        Permission.MUTE_PLAYER, Permission.WARN_PLAYER, Permission.KICK_PLAYER,
        Permission.TEMP_BAN_PLAYER, Permission.BAN_PLAYER, Permission.MANAGE_ROOMS,
        Permission.START_GAME, Permission.STOP_GAME, Permission.USE_DEBUG,
    },
    AdminLevel.SENIOR_ADMIN: {
        Permission.VIEW_PROFILE, Permission.VIEW_PLAYERS, Permission.VIEW_STATS,
        Permission.WARN_PLAYER, Permission.MUTE_PLAYER, Permission.KICK_PLAYER,
        Permission.BAN_PLAYER, Permission.MANAGE_ROOMS, Permission.START_GAME,
        Permission.STOP_GAME, Permission.USE_DEBUG, Permission.MANAGE_SETTINGS,
        Permission.MANAGE_ROLES, Permission.BROADCAST, Permission.MANAGE_STAFF,
        Permission.MANAGE_GROUP,
    },
    AdminLevel.OWNER: set(Permission),  # все права
}


@dataclass
class ResolvedAccess:
    level: AdminLevel
    is_global: bool  # выдан глобально (OWNER_IDS/ADMIN_IDS), действует везде

    @property
    def title(self) -> str:
        return LEVEL_TITLES.get(self.level, "👤 Player")

    @property
    def permissions(self) -> set[Permission]:
        return LEVEL_PERMISSIONS.get(self.level, set())


class PermissionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------ глобальое

    def is_global_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.settings.owner_id_list()

    def global_level(self, telegram_id: int) -> AdminLevel:
        if self.is_global_owner(telegram_id):
            return AdminLevel.OWNER
        if self.settings.is_admin(telegram_id):
            return AdminLevel.SENIOR_ADMIN
        return AdminLevel.PLAYER

    # ------------------------------------------------------------- локально

    async def group_level(self, session: AsyncSession, group_id: int, telegram_id: int) -> AdminLevel:
        """Локальный уровень в группе (права других групп не учитываются)."""
        from bot.database.repositories.users import UserRepository

        user = await UserRepository(session).get_by_telegram_id(telegram_id)
        if user is None:
            return AdminLevel.PLAYER
        level = await GroupAdminRepository(session).level_of(group_id, user.id)
        return AdminLevel(level)

    async def resolve(
        self, session: AsyncSession, telegram_id: int, group_id: int | None
    ) -> ResolvedAccess:
        """Эффективный уровень в контексте чата."""
        global_level = self.global_level(telegram_id)
        if global_level >= AdminLevel.SENIOR_ADMIN:
            # глобальные права действуют во всех чатах
            return ResolvedAccess(global_level, is_global=True)
        if group_id is not None:
            local = await self.group_level(session, group_id, telegram_id)
            return ResolvedAccess(local, is_global=False)
        return ResolvedAccess(AdminLevel.PLAYER, is_global=False)

    async def has(
        self,
        session: AsyncSession,
        telegram_id: int,
        group_id: int |None,
        permission: Permission,
    ) -> bool:
        access = await self.resolve(session, telegram_id, group_id)
        return permission in access.permissions

    # --------------------------------------------------- защита иерархии

    def can_moderate(self, actor: ResolvedAccess, target_level: AdminLevel, same_user: bool) -> tuple[bool, str]:
        """Модерация (warn/mute/kick/ban): нельзя карать себя и уровень >= своего.
        Владелец (5) может всё, кроме другого владельца."""
        if same_user:
            return False, "Нельзя применить это к самому себе."
        if target_level >= actor.level:
            return False, "Нельзя применять к администратору с уровнем выше или равным вашему."
        return True, "OK"


    def can_manage_staff_level(
        self, actor: ResolvedAccess, target_current: AdminLevel, new_level: AdminLevel, same_user: bool
    ) -> tuple[bool, str]:
        """Правила §21: нельзя повышать себя, выдавать уровень >= своего,
        менять старшего/равного, снимать Owner."""
        if same_user:
            return False, "Нельзя изменять собственный уровень."
        if new_level >= actor.level:
            return False, "Нельзя выдать уровень выше или равный своему."
        if target_current >= actor.level:
            return False, "Нельзя изменять администратора с уровнем выше или равным вашему."
        if target_current == AdminLevel.OWNER or new_level == AdminLevel.OWNER:
            return False, "Уровень Owner назначается только через OWNER_IDS в .env."
        return True, "OK"
