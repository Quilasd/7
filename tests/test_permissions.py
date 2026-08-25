"""Многоуровневая администрация: права по уровням, изоляция групп, защита штаба.

Требования спека:
- уровни 0..5 (Player/Helper/Moderator/Admin/Senior Admin/Owner);
- глобальный Owner (OWNER_IDS) — полные права везде, включая DEBUG;
- локальные админы действуют ТОЛЬКО в своей группе (A-админ ≠ B-админ);
- нельзя повышать себя, выдавать уровень >= своего, менять равного/старшего,
  снимать Owner; все действия пишутся в AuditLog.
"""

from __future__ import annotations

from bot.database.repositories.groups import AuditLogRepository, GroupAdminRepository
from bot.services.permissions import AdminLevel, Permission, PermissionService
from tests.conftest import make_user


class TestLevelPermissions:
    def test_player_has_nothing(self):
        from bot.services.permissions import LEVEL_PERMISSIONS

        assert LEVEL_PERMISSIONS[AdminLevel.PLAYER] == set()

    def test_progressive_permissions(self):
        from bot.services.permissions import LEVEL_PERMISSIONS as LP

        # мут — у Helper (обратимая мера), варна у Helper больше нет
        assert Permission.MUTE_PLAYER in LP[AdminLevel.HELPER]
        assert Permission.WARN_PLAYER not in LP[AdminLevel.HELPER]
        # Moderator: варн + кик + ВРЕМЕННЫЙ бан, постоянного бана НЕТ
        assert Permission.WARN_PLAYER in LP[AdminLevel.MODERATOR]
        assert Permission.KICK_PLAYER in LP[AdminLevel.MODERATOR]
        assert Permission.TEMP_BAN_PLAYER in LP[AdminLevel.MODERATOR]
        assert Permission.BAN_PLAYER not in LP[AdminLevel.MODERATOR]
        assert Permission.MANAGE_ROOMS not in LP[AdminLevel.MODERATOR]
        assert Permission.USE_DEBUG not in LP[AdminLevel.MODERATOR]
        # Admin: + постоянный бан
        assert Permission.BAN_PLAYER in LP[AdminLevel.ADMIN]
        assert {Permission.MANAGE_ROOMS, Permission.START_GAME, Permission.STOP_GAME,
                Permission.USE_DEBUG} <= LP[AdminLevel.ADMIN]
        assert Permission.MANAGE_SETTINGS not in LP[AdminLevel.ADMIN]
        assert {Permission.MANAGE_SETTINGS, Permission.MANAGE_STAFF, Permission.BROADCAST,
                Permission.MANAGE_GROUP} <= LP[AdminLevel.SENIOR_ADMIN]
        assert Permission.MANAGE_GLOBAL_SETTINGS not in LP[AdminLevel.SENIOR_ADMIN]
        assert LP[AdminLevel.OWNER] == set(Permission)


class TestResolution:
    async def test_global_owner_full_rights_everywhere(self, services, session):
        services.settings._owners = [999999]
        group_a = await services.groups.get_or_create(-200100, "A")
        group_b = await services.groups.get_or_create(-200200, "B")
        for gid in (group_a.id, group_b.id, None):
            access = await services.permissions.resolve(session, 999999, gid)
            assert access.level == AdminLevel.OWNER and access.is_global
            assert Permission.MANAGE_GLOBAL_SETTINGS in access.permissions

    async def test_global_admin_ids_senior_everywhere(self, services, session):
        services.settings._admins = [888888]
        group = await services.groups.get_or_create(-200300, "A")
        access = await services.permissions.resolve(session, 888888, group.id)
        assert access.level == AdminLevel.SENIOR_ADMIN and access.is_global

    async def test_local_admin_only_in_own_group(self, services, session):
        boss = await make_user(session, "LocalBoss")
        a = await services.groups.get_or_create(-200400, "A")
        b = await services.groups.get_or_create(-200500, "B")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(a.id, boss.id, AdminLevel.ADMIN.value, 0)
            await s.commit()

        # в группе A — админ
        access_a = await services.permissions.resolve(session, boss.telegram_id, a.id)
        assert access_a.level == AdminLevel.ADMIN and not access_a.is_global
        assert await services.permissions.has(
            session, boss.telegram_id, a.id, Permission.STOP_GAME
        )
        # в группе B — обычный игрок (A-админ ≠ B-админ)
        access_b = await services.permissions.resolve(session, boss.telegram_id, b.id)
        assert access_b.level == AdminLevel.PLAYER
        assert not await services.permissions.has(
            session, boss.telegram_id, b.id, Permission.STOP_GAME
        )
        # в личке — обычный игрок
        access_private = await services.permissions.resolve(session, boss.telegram_id, None)
        assert access_private.level == AdminLevel.PLAYER

    async def test_helper_cannot_use_debug(self, services, session):
        helper = await make_user(session, "Helper")
        group = await services.groups.get_or_create(-200600, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, helper.id, AdminLevel.HELPER.value, 0)
            await s.commit()
        assert not await services.permissions.has(
            session, helper.telegram_id, group.id, Permission.USE_DEBUG
        )


class TestStaffProtection:
    def _svc(self) -> PermissionService:
        return PermissionService.__new__(PermissionService)

    def test_self_promotion_blocked(self):
        ok, _why = self._svc().can_manage_staff_level(
            actor=_A(AdminLevel.SENIOR_ADMIN),
            target_current=AdminLevel.PLAYER,
            new_level=AdminLevel.MODERATOR,
            same_user=True,
        )
        assert not ok

    def test_granting_level_ge_own_blocked(self):
        ok, _ = self._svc().can_manage_staff_level(
            _A(AdminLevel.SENIOR_ADMIN), AdminLevel.PLAYER, AdminLevel.SENIOR_ADMIN, False
        )
        assert not ok

    def test_modifying_equal_or_higher_blocked(self):
        ok, _ = self._svc().can_manage_staff_level(
            _A(AdminLevel.ADMIN), AdminLevel.ADMIN, AdminLevel.HELPER, False
        )
        assert not ok

    def test_owner_level_only_via_env(self):
        # нельзя НАЗНАЧить Owner
        ok, _why = self._svc().can_manage_staff_level(
            _A(AdminLevel.OWNER), AdminLevel.PLAYER, AdminLevel.OWNER, False
        )
        assert not ok
        # нельзя СНЯть Owner
        ok2, _ = self._svc().can_manage_staff_level(
            _A(AdminLevel.OWNER), AdminLevel.OWNER, AdminLevel.PLAYER, False
        )
        assert not ok2

    def test_valid_grant_allowed(self):
        ok, _ = self._svc().can_manage_staff_level(
            _A(AdminLevel.SENIOR_ADMIN), AdminLevel.PLAYER, AdminLevel.MODERATOR, False
        )
        assert ok

    async def test_set_staff_integration_and_audit(self, services, session):
        actor = await make_user(session, "Chief")   # уровень 4 в группе
        target = await make_user(session, "Rookie")
        group = await services.groups.get_or_create(-200700, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, actor.id, 4, 0)
            await s.commit()

        # может выдать 3
        ok, _ = await services.groups.set_staff(
            group.id, actor.telegram_id, AdminLevel.SENIOR_ADMIN,
            target.id, AdminLevel.ADMIN.value, actor.id,
        )
        assert ok
        assert await services.permissions.group_level(session, group.id, target.telegram_id) \
            == AdminLevel.ADMIN

        # не может выдать 4 (равный свой уровень)
        ok2, why2 = await services.groups.set_staff(
            group.id, actor.telegram_id, AdminLevel.SENIOR_ADMIN,
            target.id, AdminLevel.SENIOR_ADMIN.value, actor.id,
        )
        assert not ok2 and "равн" in why2

        # не может изменить уже-админа уровня 3, будучи 4? может (3 < 4) — но не снять равного 4:
        other = await make_user(session, "Peer")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, other.id, 4, 0)
            await s.commit()
        ok3, why3 = await services.groups.remove_staff(
            group.id, AdminLevel.SENIOR_ADMIN, other.id, actor.id
        )
        assert not ok3 and "равн" in why3

        # аудит: назначение записано
        async with services.session_factory() as s:
            logs = await AuditLogRepository(s).last(group_id=group.id, limit=10)
        assert any(l.action == "staff_set_level" and l.target_id == target.id for l in logs)

    async def test_remove_staff(self, services, session):
        actor = await make_user(session, "Chief2")
        target = await make_user(session, "Temp")
        group = await services.groups.get_or_create(-200800, "A")
        async with services.session_factory() as s:
            await GroupAdminRepository(s).set_level(group.id, actor.id, 4, 0)
            await GroupAdminRepository(s).set_level(group.id, target.id, 1, 0)
            await s.commit()

        ok, _ = await services.groups.remove_staff(
            group.id, AdminLevel.SENIOR_ADMIN, target.id, actor.id
        )
        assert ok
        assert await services.permissions.group_level(session, group.id, target.telegram_id) \
            == AdminLevel.PLAYER


class _A:
    """Мини-двойник ResolvedAccess (нужен только .level)."""

    def __init__(self, level: AdminLevel) -> None:
        self.level = level
