"""Справка по командам: /cmdhelp (игрокам) и /acmdhelp (админам).

/cmdhelp — команды обычных игроков (работает и в ЛС, и в группе).
/acmdhelp — административная справка строго по ФАКТИЧЕСКОМУ уровню
пользователя: локальный Lv.1–Lv.4 — относительно текущей группы
(chat_id), глобальный Owner Lv.5 — везде (OWNER_IDS, без локальных
записей). В ЛС локальные права групп НЕ применяются: обычному игроку —
отказ без раскрытия списка.

Справка не создаёт записей в БД и не является проверкой прав: каждый
хендлер продолжает сам проверять свои permissions (ТЗ «Security»).

Источник списка — bot/utils/command_registry.py (единый реестр,
пороги уровней вычисляются из LEVEL_PERMISSIONS).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.permissions import AdminLevel
from bot.utils.command_registry import admin_help_text, player_help_text

logger = logging.getLogger(__name__)
router = Router(name="cmdhelp")


@router.message(Command("cmdhelp"))
async def cmd_cmdhelp(message: Message) -> None:
    """Справка для обычных игроков: только игровые команды."""
    in_group = message.chat.type in ("group", "supergroup")
    await message.answer(player_help_text(in_group=in_group))


@router.message(Command("acmdhelp"))
async def cmd_acmdhelp(message: Message, session, services, group=None) -> None:
    """Административная справка по реальному уровню пользователя.

    Уровень определяется существующим PermissionService.resolve()
    относительно ТЕКУЩЕГО чата: в группе — локальный уровень этой группы
    (или глобальный, если выдан .env); в ЛС — только глобальный.
    Права других групп не используются.
    """
    access = await services.permissions.resolve(
        session, message.from_user.id, group.id if group is not None else None
    )
    if access.level < AdminLevel.HELPER:
        await message.answer("❌ Эта команда доступна только администраторам.")
        return

    await message.answer(
        admin_help_text(
            level=int(access.level),
            is_global=access.is_global,
            in_group=group is not None,
        )
    )
