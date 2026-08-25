"""Социальные команды: друзья, запросы, игнор-лист, избранное, приглашения.

Поиск цели переиспользует существующий UserLookupService
(Telegram ID / @username / reply). Запросы в друзья с inline-кнопками
принять/отклонить. Игнор применяется к приглашениям (/invite).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.repositories.rooms import RoomPlayerRepository, RoomRepository
from bot.services.lookup import UserLookupService
from bot.utils.callbacks import SocialCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


def _resolve(session, message: Message, args: CommandObject):
    return UserLookupService(session).resolve(
        query=args.args if args and args.args else None,
        reply_telegram_id=(
            message.reply_to_message.from_user.id
            if message.reply_to_message and message.reply_to_message.from_user
            else None
        ),
    )


def _friends_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Запросы", callback_data=SocialCB(action="requests").pack()),
        InlineKeyboardButton(text="⭐ Избранные", callback_data=SocialCB(action="favorites").pack()),
        InlineKeyboardButton(text="🚫 Игнор", callback_data=SocialCB(action="ignored").pack()),
    ]])


def _requests_kb(requests) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for u in requests:
        rows.append([
            InlineKeyboardButton(text=f"✅ {display_name(u)}",
                                 callback_data=SocialCB(action="accept", value=str(u.id)).pack()),
            InlineKeyboardButton(text="❌",
                                 callback_data=SocialCB(action="decline", value=str(u.id)).pack()),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------- друзья

@router.message(Command("friends"))
async def cmd_friends(message: Message, services, db_user) -> None:
    friends = await services.social.friends_of(db_user.id)
    if not friends:
        await message.answer("👥 У тебя пока нет друзей.\n\nОтправь запрос: <code>/friend @username</code>", reply_markup=_friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in friends)
    await message.answer(f"👥 <b>Друзья ({len(friends)})</b>\n\n{names}", reply_markup=_friends_kb(db_user.id))


@router.message(Command("friend", "addfriend", "fadd"))
async def cmd_friend(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого добавить? <code>/friend @username</code> или ответом на сообщение.")
        return
    ok, msg = await services.social.send_request(db_user.id, target.id)
    if ok:
        try:
            await services.notifier.send(
                target.telegram_id,
                f"📨 <b>{esc(display_name(db_user))}</b> хочет добавить тебя в друзья.\n\n"
                f"Прими: <code>/accept {db_user.telegram_id}</code> или отклони: <code>/decline {db_user.telegram_id}</code>",
            )
        except Exception:  # noqa: BLE001 — уведомление не критично
            logger.debug("Не удалось уведомить %s о запросе в друзья", target.id)
    await message.answer(msg)


@router.message(Command("accept"))
async def cmd_accept(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого принять? <code>/accept @username</code> или ответом на сообщение.")
        return
    ok, msg = await services.social.accept_request(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("decline"))
async def cmd_decline(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Какой запрос отклонить? <code>/decline @username</code>")
        return
    ok, msg = await services.social.decline_request(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("unfriend", "fremove"))
async def cmd_unfriend(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого удалить из друзей? <code>/unfriend @username</code>")
        return
    ok, msg = await services.social.remove_friend(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("requests"))
async def cmd_requests(message: Message, services, db_user) -> None:
    reqs = await services.social.pending_requests(db_user.id)
    if not reqs:
        await message.answer("📭 Входящих запросов в друзья нет.", reply_markup=_friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in reqs)
    await message.answer(f"📨 <b>Запросы в друзья ({len(reqs)})</b>\n\n{names}", reply_markup=_requests_kb(reqs))


# --------------------------------------------------------------- игнор

@router.message(Command("ignore", "block"))
async def cmd_ignore(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого игнорировать? <code>/ignore @username</code>")
        return
    ok, msg = await services.social.block(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("unignore", "unblock"))
async def cmd_unignore(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого убрать из игнора? <code>/unignore @username</code>")
        return
    ok, msg = await services.social.unblock(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("ignored"))
async def cmd_ignored(message: Message, services, db_user) -> None:
    users = await services.social.blocked_users(db_user.id)
    if not users:
        await message.answer("🚫 Игнор-лист пуст.", reply_markup=_friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in users)
    await message.answer(f"🚫 <b>Игнор-лист ({len(users)})</b>\n\n{names}", reply_markup=_friends_kb(db_user.id))


# --------------------------------------------------------------- избранное

@router.message(Command("favorite", "fav"))
async def cmd_favorite(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого добавить в избранное? <code>/favorite @username</code>")
        return
    ok, msg = await services.social.favorite(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("unfavorite", "unfav"))
async def cmd_unfavorite(message: Message, command: CommandObject, session, services, db_user) -> None:
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого убрать из избранного? <code>/unfavorite @username</code>")
        return
    ok, msg = await services.social.unfavorite(db_user.id, target.id)
    await message.answer(msg)


@router.message(Command("favorites"))
async def cmd_favorites(message: Message, services, db_user) -> None:
    users = await services.social.favorites_of(db_user.id)
    if not users:
        await message.answer("⭐ Список избранных пуст.", reply_markup=_friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in users)
    await message.answer(f"⭐ <b>Избранные ({len(users)})</b>\n\n{names}", reply_markup=_friends_kb(db_user.id))


# --------------------------------------------------------------- приглашение в игру

@router.message(Command("invite"))
async def cmd_invite(message: Message, command: CommandObject, session, services, db_user) -> None:
    """Пригласить игрока в свою открытую комнату. Учитывает игнор."""
    target = await _resolve(session, message, command)
    if target is None:
        await message.answer("Кого пригласить? <code>/invite @username</code>")
        return
    if target.id == db_user.id:
        await message.answer("Нельзя пригласить самого себя.")
        return
    # игнор: нельзя пригласить, кого игнорируешь, или кто игнорирует тебя
    if await services.social.is_blocked(db_user.id, target.id):
        await message.answer("Ты добавил этого игрока в игнор — сначала убери: /unignore.")
        return
    if await services.social.is_blocked(target.id, db_user.id):
        await message.answer("Этот игрок не принимает от тебя приглашения.")
        return
    # ищем открытую комнату приглашающего
    room = await RoomRepository(session).open_room_of_user(db_user.id)
    if room is None:
        await message.answer("У тебя нет открытой комнаты для приглашения. Сначала создай комнату.")
        return
    if room.player_count >= room.max_players:
        await message.answer(f"Комната #{room.id} уже заполнена ({room.max_players}).")
        return
    # нельзя пригласить того, кто уже в комнате
    already = await RoomPlayerRepository(session).get_membership(room.id, target.id)
    if already is not None:
        await message.answer(f"{esc(display_name(target))} уже в твоей комнате #{room.id}.")
        return
    if target.is_banned:
        await message.answer("Этот игрок заблокирован и не может участвовать в играх.")
        return
    if room.group_id:
        banned, gp_row = await services.groups.effective_ban(session, room.group_id, target.id)
        if banned:
            await message.answer("Этот игрок забанен в группе, к которой относится комната.")
            return
    await services.notifier.send(
        target.telegram_id,
        f"🎮 <b>{esc(display_name(db_user))}</b> приглашает тебя в комнату #{room.id} "
        f"(<i>{esc(room.name or 'Игра в мафию')}</i>).\n\n"
        f"Присоединиться: <code>/join {room.id}</code>",
    )
    await message.answer(f"📨 Приглашение отправлено: {esc(display_name(target))}.")


# --------------------------------------------------------------- callback-роутеры

@router.callback_query(SocialCB.filter(F.action == "requests"))
async def cb_requests(callback: CallbackQuery, services, db_user) -> None:
    await callback.answer()
    reqs = await services.social.pending_requests(db_user.id)
    if not reqs:
        await edit_or_answer(callback, "📭 Входящих запросов в друзья нет.", _friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in reqs)
    await edit_or_answer(callback, f"📨 <b>Запросы в друзья ({len(reqs)})</b>\n\n{names}", _requests_kb(reqs))


@router.callback_query(SocialCB.filter(F.action.in_({"accept", "decline"})))
async def cb_accept_decline(callback: CallbackQuery, callback_data: SocialCB, services, db_user) -> None:
    try:
        from_id = int(callback_data.value)
    except (TypeError, ValueError):
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    if callback_data.action == "accept":
        ok, msg = await services.social.accept_request(db_user.id, from_id)
    else:
        ok, msg = await services.social.decline_request(db_user.id, from_id)
    await callback.answer(msg[:180], show_alert=not ok)
    reqs = await services.social.pending_requests(db_user.id)
    if reqs:
        names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in reqs)
        await edit_or_answer(callback, f"📨 <b>Запросы в друзья ({len(reqs)})</b>\n\n{names}", _requests_kb(reqs))
    else:
        await edit_or_answer(callback, "📭 Запросов больше нет.", _friends_kb(db_user.id))


@router.callback_query(SocialCB.filter(F.action == "favorites"))
async def cb_favorites(callback: CallbackQuery, services, db_user) -> None:
    await callback.answer()
    users = await services.social.favorites_of(db_user.id)
    if not users:
        await edit_or_answer(callback, "⭐ Список избранных пуст.", _friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in users)
    await edit_or_answer(callback, f"⭐ <b>Избранные ({len(users)})</b>\n\n{names}", _friends_kb(db_user.id))


@router.callback_query(SocialCB.filter(F.action == "ignored"))
async def cb_ignored(callback: CallbackQuery, services, db_user) -> None:
    await callback.answer()
    users = await services.social.blocked_users(db_user.id)
    if not users:
        await edit_or_answer(callback, "🚫 Игнор-лист пуст.", _friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in users)
    await edit_or_answer(callback, f"🚫 <b>Игнор-лист ({len(users)})</b>\n\n{names}", _friends_kb(db_user.id))


@router.callback_query(SocialCB.filter(F.action == "friends"))
async def cb_friends(callback: CallbackQuery, services, db_user) -> None:
    await callback.answer()
    friends = await services.social.friends_of(db_user.id)
    if not friends:
        await edit_or_answer(callback, "👥 У тебя пока нет друзей.", _friends_kb(db_user.id))
        return
    names = "\n".join(f"• {esc(display_name(u))} <code>({u.telegram_id})</code>" for u in friends)
    await edit_or_answer(callback, f"👥 <b>Друзья ({len(friends)})</b>\n\n{names}", _friends_kb(db_user.id))
