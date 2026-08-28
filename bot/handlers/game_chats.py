"""Настройка игровых форумов (Game Forum / Mafia Forum) — PER-GROUP (ТЗ-11).

Темы партии создаются БОТОМ автоматически при старте игры в форумных чатах
ГРУППЫ этой игры. Глобальные env-форумы — только fallback для игр без группы.

- В ГРУППЕ: <code>/set_game_forum &lt;chat_id&gt;</code> — глобальный Owner или
  локальный Senior Admin+ (MANAGE_SETTINGS) ЭТОЙ группы; пишет в
  group_settings группы. Игры группы A никогда не используют форумы группы B.
- В ЛС (только глобальный Owner): <code>/set_game_forum &lt;chat_id&gt;</code> —
  настраивает глобальный fallback для игр без группы.

Модерация тем партий — GameChatGuardMiddleware (bot/middlewares).
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

router = Router(name="game_forums")


async def _set_forum(message: Message, session, services, db_user, kind: str, group=None) -> None:
    settings = services.settings
    is_owner = (
        db_user is not None
        and settings is not None
        and bool(getattr(settings, "is_owner", lambda _tid: False)(db_user.telegram_id))
    )
    in_group = group is not None

    # ---- права: в группе — owner ИЛИ локальный Senior Admin+ (MANAGE_SETTINGS)
    if in_group:
        if not is_owner:
            from bot.services.permissions import Permission

            access = await services.permissions.resolve(
                session, message.from_user.id, group.id
            )
            if Permission.MANAGE_SETTINGS not in access.permissions:
                await message.answer(
                    "⚙️ Настройку форумов группы может выполнять только владелец бота "
                    "или локальный старший администратор этой группы."
                )
                return
    elif not is_owner:
        await message.answer("⚙️ Глобальные форумы настраивает только владелец бота.")
        return

    # ---- chat_id: только аргумент (форум — отдельный чат, не этот)
    parts = (message.text or "").split()
    chat_id = None
    if len(parts) > 1 and parts[1].lstrip("-").isdigit():
        chat_id = int(parts[1])
    if chat_id is None:
        where = (
            f"Формат: <code>/{'set_game_forum' if kind == 'game' else 'set_mafia_forum'} "
            "&lt;chat_id форума&gt;</code> — ID форумного чата, темы которого будет "
            "использовать эта группа."
            if in_group else
            f"Формат: <code>/{'set_game_forum' if kind == 'game' else 'set_mafia_forum'} "
            "&lt;chat_id&gt;</code> — ID форумного чата для игр вне групп."
        )
        await message.answer(where)
        return

    label = "Game Forum" if kind == "game" else "Mafia Forum"

    if in_group:
        # per-group: пишем в group_settings ЭТОЙ группы
        from bot.database.repositories.groups import GroupSettingsRepository

        gs = await GroupSettingsRepository(session).get_or_create(group.id)
        if kind == "game":
            gs.game_forum_chat_id = chat_id
        else:
            gs.mafia_forum_chat_id = chat_id
        await session.commit()
        await message.answer(
            f"⚙️ {label} группы «{group.title or group.id}»: <code>{chat_id}</code>\n\n"
            "Новые партии этой группы будут создавать темы в этом форуме.\n"
            "Убедитесь: это супергруппа-форум (включены темы) и бот — администратор "
            "с правом <b>can_manage_topics</b>."
        )
        logger.info(
            "Группа %s: настроен %s=%s (user %s)", group.id, label, chat_id, db_user.id
        )
        return

    # глобальный fallback (игры без группы)
    app_config = getattr(services, "app_config", None)
    if app_config is None:
        await message.answer("Конфигурация недоступна.")
        return
    gs = await app_config.get()
    if kind == "game":
        gs.game_forum_chat_id = chat_id
    else:
        gs.mafia_forum_chat_id = chat_id
    await app_config.save(gs)

    # сразу проверяем доступ
    forums = await services.game_chats.check_forums()
    info = forums[kind]
    status = "✅ форум доступен" if info["ok"] else f"⚠️ {info.get('error', 'проблема')}"
    await message.answer(
        f"⚙️ {label} настроен (игры вне групп): <code>{chat_id}</code>\n{status}\n\n"
        + ("" if info["ok"] else
           "Убедитесь: это супергруппа-форум (включены темы) и бот — администратор "
           "с правом <b>can_manage_topics</b>.")
    )
    logger.info("Настроен глобальный %s: %s (user %s)", label, chat_id, db_user.id)


@router.message(Command("set_game_forum"))
async def cmd_set_game_forum(message: Message, session, services, db_user, group=None) -> None:
    await _set_forum(message, session, services, db_user, "game", group=group)


@router.message(Command("set_mafia_forum"))
async def cmd_set_mafia_forum(message: Message, session, services, db_user, group=None) -> None:
    await _set_forum(message, session, services, db_user, "mafia", group=group)
