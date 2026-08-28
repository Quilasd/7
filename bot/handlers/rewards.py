"""Ивентовые награды и титулы: админ-выдача + выбор активных.

Админ-команды (только глобальная администрация: OWNER_IDS + ADMIN_IDS):
    /reward_create code|emoji|name|kind|expires_days|описание
    /reward_grant  @username|id  code  [expires_days]
    /reward_list                  — каталог наград
    /title_grant  @username|id  title_id

Команды для всех (только над своими данными):
    /titles               — мои титулы
    /title_set  title_id  — выбрать активный титул
    /rewards              — мои ивентовые награды
    /reward_activate id   — выбрать активную награду
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.services import titles as ttl
from bot.services.lookup import UserLookupService
from bot.services.permissions import AdminLevel
from bot.utils.helpers import display_name, esc

logger = logging.getLogger(__name__)
router = Router()


def _is_global_admin(services, telegram_id: int) -> bool:
    return services.permissions.global_level(telegram_id) >= AdminLevel.ADMIN


def _resolve(session, message: Message, args: CommandObject):
    return UserLookupService(session).resolve(
        query=args.args.split()[0] if (args and args.args and args.args.split()) else None,
        reply_telegram_id=(
            message.reply_to_message.from_user.id
            if message.reply_to_message and message.reply_to_message.from_user
            else None
        ),
    )


# --------------------------------------------------- админ: ивентовые награды

@router.message(Command("reward_create"))
async def cmd_reward_create(message: Message, command: CommandObject, services, db_user) -> None:
    if not _is_global_admin(services, message.from_user.id):
        return
    parts = (command.args or "").split("|")
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/reward_create code|emoji|name|kind|expires_days|описание</code>\n"
            "kind: event | tournament | role | special. expires_days — число или пусто (бессрочно)."
        )
        return
    code = parts[0].strip()
    emoji = parts[1].strip() or "🎁"
    name = parts[2].strip()
    kind = parts[3].strip() if len(parts) > 3 else "event"
    expires = None
    if len(parts) > 4 and parts[4].strip().isdigit():
        expires = int(parts[4].strip())
    description = parts[5].strip() if len(parts) > 5 else ""
    ok, msg = await services.rewards.create_reward(
        code, name, emoji, description, kind, expires, db_user.id
    )
    await message.answer(msg)


@router.message(Command("reward_grant"))
async def cmd_reward_grant(message: Message, command: CommandObject, session, services, db_user) -> None:
    if not _is_global_admin(services, message.from_user.id):
        return
    tokens = (command.args or "").split()
    if len(tokens) < 2:
        await message.answer("Формат: <code>/reward_grant @username|id code [expires_days]</code>")
        return
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Получатель не найден.")
        return
    code = tokens[1]
    expires = int(tokens[2]) if len(tokens) > 2 and tokens[2].isdigit() else None
    ok, msg = await services.rewards.grant(target.id, code, db_user.id, expires_days=expires)
    await message.answer(msg)


@router.message(Command("reward_list"))
async def cmd_reward_list(message: Message, services) -> None:
    catalog = await services.rewards.list_catalog()
    if not catalog:
        await message.answer("🎪 Каталог ивентовых наград пуст.")
        return
    lines = ["🎪 <b>КАТАЛОГ НАГРАД</b>", ""]
    for r in catalog:
        exp = f", {r.expires_days} дн." if r.expires_days else ", бессрочно"
        lines.append(f"{r.emoji} <b>{esc(r.name)}</b> <code>({r.code})</code> [{r.kind}{exp}]")
        if r.description:
            lines.append(f"    <i>{esc(r.description)}</i>")
    await message.answer("\n".join(lines))


# --------------------------------------------------- админ: титулы

@router.message(Command("title_grant"))
async def cmd_title_grant(message: Message, command: CommandObject, session, services) -> None:
    if not _is_global_admin(services, message.from_user.id):
        return
    tokens = (command.args or "").split()
    if len(tokens) < 2:
        from bot.services import titles as t
        ids = ", ".join(x.id for x in t._ALL_TITLES)  # noqa: SLF001
        await message.answer(f"Формат: <code>/title_grant @username|id title_id</code>\nДоступно: {ids}")
        return
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Получатель не найден.")
        return
    title_id = tokens[1]
    from bot.services import rewards as rw
    ok = await rw.grant_title(session, target.id, title_id, source="admin")
    if ok:
        await session.commit()
        title = ttl.get_title(title_id)
        await message.answer(f"🎓 Титул «{title.name if title else title_id}» выдан: {esc(display_name(target))}.")
    else:
        await message.answer("Неизвестный титул.")


@router.message(Command("title_list"))
async def cmd_title_list(message: Message, services) -> None:
    """Каталог титулов (глобальная администрация)."""
    if not _is_global_admin(services, message.from_user.id):
        return
    from bot.services import titles as t

    unlocks = {v: k for k, v in t.TITLE_UNLOCKS.items()}
    lines = ["🎓 <b>КАТАЛОГ ТИТУЛОВ</b>", ""]
    for title in t._ALL_TITLES:  # noqa: SLF001
        src = unlocks.get(title.id)
        how = f"за достижение <code>{src}</code>" if src else "выдаётся админом/ивентом"
        lines.append(f"{title.emoji} <b>{title.name}</b> <code>({title.id})</code> — {how}")
    await message.answer("\n".join(lines))


@router.message(Command("title_remove"))
async def cmd_title_remove(message: Message, command: CommandObject, session, services) -> None:
    """Снять выданный титул (глобальная администрация)."""
    if not _is_global_admin(services, message.from_user.id):
        return
    tokens = (command.args or "").split()
    if len(tokens) < 2:
        await message.answer("Формат: <code>/title_remove @username|id title_id</code>")
        return
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Игрок не найден.")
        return
    from bot.database.repositories.social import UserTitleRepository

    removed = await UserTitleRepository(session).remove(target.id, tokens[1])
    if target.active_title == tokens[1]:
        target.active_title = None
    await session.commit()
    if removed:
        title = ttl.get_title(tokens[1])
        name = title.name if title else tokens[1]
        await message.answer(f"🎓 Титул «{name}» снят с {esc(display_name(target))}.")
    else:
        await message.answer("У игрока нет такого титула.")


# --------------------------------------------------- игроки: свои титулы/награды

@router.message(Command("titles"))
async def cmd_titles(message: Message, session, db_user) -> None:
    from bot.services import rewards as rw
    unlocked = await rw.unlocked_titles(session, db_user.id)
    active = ttl.get_title(db_user.active_title)
    head = f"🎓 Активный: <b>{active.name if active else '—'}</b>\n\n"
    if not unlocked:
        await message.answer(head + "Открытых титулов пока нет.")
        return
    names = "\n".join(f"• {t.emoji} {t.name} <code>({t.id})</code>" for t in unlocked)
    await message.answer(head + f"<b>Открытые титулы ({len(unlocked)}):</b>\n{names}\n\nВыбрать: <code>/title_set id</code>")


@router.message(Command("title_set"))
async def cmd_title_set(message: Message, command: CommandObject, session, db_user) -> None:
    from bot.services import rewards as rw
    title_id = (command.args or "").strip()
    if not title_id:
        await message.answer("Укажи титул: <code>/title_set id</code> (список — /titles).")
        return
    ok = await rw.set_active_title(session, db_user.id, title_id)
    await session.commit()
    title = ttl.get_title(title_id)
    await message.answer(
        f"🎓 Активный титул: <b>{title.name if title else title_id}</b>." if ok
        else "Титул не открыт или не существует."
    )


@router.message(Command("rewards"))
async def cmd_rewards(message: Message, services, db_user) -> None:
    rows = await services.rewards.user_rewards(db_user.id)
    if not rows:
        await message.answer("🎪 У тебя пока нет ивентовых наград.")
        return
    from bot.utils.helpers import utcnow
    lines = ["🎪 <b>МОИ ИВЕНТОВЫЕ НАГРАДЫ</b>", ""]
    for r in rows:
        reward = r.reward
        expired = r.expires_at is not None and r.expires_at <= utcnow()
        mark = "⏳" if expired else ("✅" if r.id == db_user.active_event_reward_id else "•")
        exp = f" (до {r.expires_at:%d.%m.%Y})" if r.expires_at else ""
        lines.append(f"{mark} {reward.emoji} <b>{esc(reward.name)}</b> [{reward.kind}]{exp} <code>#{r.id}</code>")
    lines.append("\nАктивировать: <code>/reward_activate id</code>")
    await message.answer("\n".join(lines))


@router.message(Command("reward_activate"))
async def cmd_reward_activate(message: Message, command: CommandObject, services, db_user) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Укажи ID награды: <code>/reward_activate id</code> (список — /rewards).")
        return
    ok, msg = await services.rewards.set_active(db_user.id, int(arg))
    await message.answer(msg)
