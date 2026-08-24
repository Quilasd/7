"""Групповые админ-команды: модерация, персонал, настройки, игры и комнаты.

Все проверки прав — через PermissionService относительно ТЕКУЩЕГО чата
(права группы A не действуют в группе B). Действия пишутся в AuditLog.

Команды:
  Профили:  /player /players /player_stats /stats
  Модерация:/warn /unwarn /warnings /mute /unmute /kick /ban /unban
  Игра:     /game /games /game_info /game_players /game_phase
            /game_start /game_stop /game_cancel /game_kill /game_revive
  Комнаты:  /rooms /room /room_close /room_kick /room_force_start
  Персонал: /staff /staff_add /staff_remove /staff_promote /staff_demote /staff_info
  Настройки:/settings /set_min_players /set_max_players /set_night_time
            /set_day_time /set_vote_time /set_roles
  Массовые: /broadcast /announce
  Система:  /botstats /logs /reload /maintenance
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.models import Group, PlayerStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.groups import GroupAdminRepository, GroupPlayerRepository
from bot.database.repositories.rooms import RoomRepository
from bot.services.lookup import UserLookupService
from bot.services.permissions import AdminLevel, LEVEL_TITLES, Permission
from bot.utils.callbacks import SettingCB
from bot.utils.helpers import display_name, esc, utcnow

logger = logging.getLogger(__name__)
router = Router()


# ------------------------------------------------------------------ helpers

async def _access(session, services, telegram_id: int, group: Group | None):
    return await services.permissions.resolve(
        session, telegram_id, group.id if group else None
    )


def _deny(event, permission: Permission):
    text = f"⛔️ Нет права {permission.value}"
    if isinstance(event, CallbackQuery):
        return event.answer(text, show_alert=True)
    return event.answer(text)


async def _require(session, services, event, group: Group | None, permission: Permission):
    """None = отказ (уже отвечен), иначе ResolvedAccess."""
    access = await _access(session, services, event.from_user.id, group)
    if permission not in access.permissions:
        await _deny(event, permission)
        return None
    return access


async def _resolve_target(message: Message, session, args: str | None):
    """Цель команды: аргумент (ID/@username) или reply."""
    reply_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        reply_id = message.reply_to_message.from_user.id
    lookup = UserLookupService(session)
    return await lookup.resolve(args or None, reply_telegram_id=reply_id)


def _group_or_reply(event, group: Group | None):
    if group is None and isinstance(event, Message):
        return "Эта команда работает только внутри группы.", False
    return None, True


# ------------------------------------------------------------------ профили

@router.message(Command("player", "player_stats"))
async def cmd_player(message: Message, command: CommandObject, session, db_user, group, services) -> None:
    from bot.handlers.profile import profile_group_block, profile_text

    args = (command.args or "").strip()
    target = db_user
    if args or message.reply_to_message:
        found = await _resolve_target(message, session, args or None)
        if found is None:
            await message.answer("🤷 Игрок не найден. Укажи ID, @username или ответь на его сообщение.")
            return
        if found.id != db_user.id:
            # Чужой профиль — только с правом VIEW_PROFILE
            access = await _access(session, services, message.from_user.id, group)
            if Permission.VIEW_PROFILE not in access.permissions:
                await _deny(message, Permission.VIEW_PROFILE)
                return
        target = found
    await message.answer(f"<b>{esc(display_name(target))}</b>\n\n{profile_text(target, header=False)}")
    if group is not None:
        gp = await GroupPlayerRepository(session).get_membership(group.id, target.id)
        if gp:
            await message.answer(profile_group_block(group, gp))


@router.message(Command("players"))
async def cmd_players(message: Message, command: CommandObject, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_PLAYERS) is None:
        return
    text, ok = _group_or_reply(message, group)
    if not ok:
        await message.answer(text)
        return
    players = await GroupPlayerRepository(session).list_for_group(group.id, limit=50)
    lines = [f"👥 <b>ИГРОКИ ГРУППЫ</b> — {len(players)}", ""]
    for gp in players:
        staff = await GroupAdminRepository(session).get_for(group.id, gp.user_id)
        mark = LEVEL_TITLES.get(AdminLevel(staff.admin_level), "").split(" ", 1)[-1] if staff else ""
        ban = " 🚫" if gp.is_banned else ""
        warn = f" ⚠️{gp.warnings}" if gp.warnings else ""
        lines.append(f"• {esc(display_name(gp.user))} — 🎮{gp.games_played} ⭐{gp.rating}{warn}{ban} {mark}")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats_alias(message: Message, command: CommandObject, session, db_user, group) -> None:
    await cmd_player(message, command, session, db_user, group)


# ---------------------------------------------------------------- модерация

async def _moderation_target(message: Message, session, services, group, permission) -> tuple[object, object] | None:
    """Общая часть: проверить право, найти цель, вернуть (access, target_user) или None."""
    access = await _require(session, services, message, group, permission)
    if access is None:
        return None
    if group is None:
        await message.answer("Команда работает только в группе.")
        return None
    args = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) > 1 else ""
    target = await _resolve_target(message, session, args or None)
    if target is None:
        await message.answer("🤷 Укажи игрока: ID/@username или ответь на сообщение.")
        return None
    return access, target


@router.message(Command("warn", "unwarn", "warnings"))
async def cmd_warns(message: Message, session, group, db_user, services) -> None:
    command = message.text.split()[0][1:].split("@")[0]
    if command == "warnings":
        if await _require(session, services, message, group, Permission.VIEW_PLAYERS) is None:
            return
        target = await _resolve_target(message, session, (message.get_args() or "").strip() or None)
        if target is None:
            await message.answer("🤷 Укажи игрока.")
            return
        gp = await services.groups.local_player(group.id, target.id) if group else None
        count = gp.warnings if gp else 0
        await message.answer(f"⚠️ Предупреждений у {esc(display_name(target))}: {count}")
        return

    permission = Permission.WARN_PLAYER
    resolved = await _moderation_target(message, session, services, group, permission)
    if resolved is None:
        return
    _, target = resolved
    delta = 1 if command == "warn" else -1
    count = await services.groups.warn(group.id, target.id, db_user.id, delta)
    verb = "выдано предупреждение" if delta > 0 else "снято предупреждение"
    await message.answer(f"⚠️ {esc(display_name(target))}: {verb}. Всего: {count}")


@router.message(Command("mute", "unmute"))
async def cmd_mute(message: Message, session, group, db_user, services) -> None:
    command = message.text.split()[0][1:].split("@")[0]
    resolved = await _moderation_target(message, session, services, group, Permission.MUTE_PLAYER)
    if resolved is None:
        return
    _, target = resolved
    mute = command == "mute"
    applied = False
    if mute and group is not None:
        minutes = 60
        parts = message.text.split()
        if len(parts) >= 3 and parts[-1].isdigit():
            minutes = max(1, min(1440, int(parts[-1])))
        try:
            from datetime import datetime, timedelta, timezone

            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await message.bot.restrict_chat_member(
                chat_id=group.telegram_chat_id,
                user_id=target.telegram_id,
                until_date=until,
                can_send_messages=False,
            )
            applied = True
        except TelegramAPIError as exc:
            logger.warning("mute %s в %s не удался: %s", target.telegram_id, group.telegram_chat_id, exc)
    elif not mute and group is not None:
        try:
            await message.bot.restrict_chat_member(
                chat_id=group.telegram_chat_id,
                user_id=target.telegram_id,
                can_send_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
            )
            applied = True
        except TelegramAPIError as exc:
            logger.warning("unmute %s не удался: %s", target.telegram_id, exc)

    await services.audit.log(
        db_user.id, command, target.id, group.id if group else None,
        f"telegram_applied={applied}",
    )
    await message.answer(
        f"🔇 {esc(display_name(target))}: {'мут' if mute else 'мут снят'}"
        + (f" на {minutes} мин." if mute and applied else "")
        + ("" if applied else " (локальная запись; для мьюта в Telegram боту нужны права админа)")
    )


@router.message(Command("kick"))
async def cmd_kick(message: Message, session, group, db_user, services) -> None:
    resolved = await _moderation_target(message, session, services, group, Permission.KICK_PLAYER)
    if resolved is None:
        return
    _, target = resolved
    applied = False
    if group is not None:
        try:
            await message.bot.ban_chat_member(group.telegram_chat_id, target.telegram_id)
            await message.bot.unban_chat_member(group.telegram_chat_id, target.telegram_id)
            applied = True
        except TelegramAPIError as exc:
            logger.warning("kick не удался: %s", exc)
    await services.audit.log(db_user.id, "kick", target.id, group.id if group else None)
    await message.answer(f"👢 {esc(display_name(target))} исключён." + ("" if applied else " (нужны права админа у бота)"))


@router.message(Command("ban", "unban"))
async def cmd_ban(message: Message, session, group, db_user, services) -> None:
    command = message.text.split()[0][1:].split("@")[0]
    resolved = await _moderation_target(message, session, services, group, Permission.BAN_PLAYER)
    if resolved is None:
        return
    _, target = resolved
    banned, _ = await services.groups.set_local_ban(
        group.id, target.id, command == "ban", db_user.id
    )
    if command == "ban" and group is not None:
        try:
            await message.bot.ban_chat_member(group.telegram_chat_id, target.telegram_id)
        except TelegramAPIError as exc:
            logger.warning("telegram-ban не удался: %s", exc)
    await message.answer(
        f"🚫 {esc(display_name(target))}: {'забанен' if banned else 'разбанен'} в этой группе."
    )


# --------------------------------------------------------------------- игра

@router.message(Command("game", "games", "game_info"))
async def cmd_games(message: Message, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_STATS) is None:
        return
    games_repo = GameRepository(session)
    if group is not None:
        active = await games_repo.active_for_group(group.id)
        finished = await games_repo.finished_for_group(group.id, limit=5)
    else:
        active = await games_repo.active_games()
        finished = []
    lines = ["🎮 <b>ИГРЫ</b>", ""]
    if not active:
        lines.append("Активных игр нет.")
    for game in active:
        lines.append(f"• #{game.id}: {game.status}, день {game.day_number}")
    if finished:
        lines += ["", "Последние завершённые:"]
        for game in finished:
            lines.append(f"• #{game.id}: победитель {game.winner}")
    await message.answer("\n".join(lines))


@router.message(Command("game_players", "game_phase"))
async def cmd_game_info(message: Message, command: CommandObject, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_STATS) is None:
        return
    args = (command.args or "").strip()
    game = None
    if args.isdigit():
        game = await GameRepository(session).get(int(args))
    else:
        active_gp = await GamePlayerRepository(session).active_game_of_user(message.from_user.id)
        if active_gp:
            game = await GameRepository(session).get(active_gp.game_id)
    if game is None:
        await message.answer("Укажи ID игры: /game_info 123")
        return
    if command.command == "game_phase":
        await message.answer(f"Игра #{game.id}: фаза {game.status}, день {game.day_number}")
        return
    players = await GamePlayerRepository(session).list_for_game(game.id)
    lines = [f"🎮 <b>ИГРА #{game.id}</b> — {game.status}, день {game.day_number}", ""]
    for gp in players:
        lines.append(f"{'🟢' if gp.is_alive else '💀'} {esc(display_name(gp.user))}")
    await message.answer("\n".join(lines))


@router.message(Command("game_stop", "game_cancel"))
async def cmd_game_stop(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    if await _require(session, services, message, group, Permission.STOP_GAME) is None:
        return
    args = (command.args or "").strip()
    games_repo = GameRepository(session)
    game_id = int(args) if args.isdigit() else None
    if game_id is None and group is not None:
        active = await games_repo.active_for_group(group.id)
        game_id = active[0].id if active else None
    if game_id is None:
        await message.answer("Укажи ID игры: /game_stop 123")
        return
    game = await games_repo.get(game_id)
    if group is not None and game is not None and game.group_id not in (None, group.id):
        await message.answer("⛔️ Эта игра принадлежит другой группе.")
        return
    done = await services.phases.force_end(game_id, f"Остановлено {esc(display_name(db_user))}")
    await services.audit.log(
        db_user.id, "game_stop", None, group.id if group else None, f"game={game_id} ok={done}"
    )
    await message.answer("🏁 Игра остановлена." if done else "Игра не активна.")


@router.message(Command("game_kill", "game_revive"))
async def cmd_game_kill(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    command_name = command.command
    if await _require(session, services, message, group, Permission.MANAGE_ROOMS) is None:
        return
    args = (command.args or "").split()
    if len(args) < 2 or not args[0].isdigit():
        await message.answer("Формат: /game_kill <game_id> <ID|@username>")
        return
    game = await GameRepository(session).get(int(args[0]))
    if game is None:
        await message.answer("Игра не найдена.")
        return
    if group is not None and game.group_id not in (None, group.id):
        await message.answer("⛔️ Эта игра принадлежит другой группе.")
        return
    target = await UserLookupService(session).resolve(" ".join(args[1:]))
    if target is None:
        await message.answer("Игрок не найден.")
        return
    players_repo = GamePlayerRepository(session)
    gp = await players_repo.get_by_user(game.id, target.id)
    if gp is None:
        await message.answer("Игрок не участвует в этой игре.")
        return
    if command_name == "game_kill":
        if not gp.is_alive:
            await message.answer("Игрок уже мёртв.")
            return
        gp.is_alive = False
        gp.status = PlayerStatus.DEAD.value
        gp.died_at = utcnow()
        gp.death_cause = "admin"
        game.events = list(game.events or []) + [{
            "type": "death", "day": game.day_number, "user_id": gp.user_id,
            "cause": "admin", "killers": [],
        }]
        await session.commit()
        await services.audit.log(db_user.id, "game_kill", target.id, group.id if group else None, f"game={game.id}")
        await message.answer(f"💀 {esc(display_name(target))} убит администратором.")
    else:
        if gp.is_alive:
            await message.answer("Игрок жив.")
            return
        gp.is_alive = True
        gp.status = PlayerStatus.ALIVE.value
        gp.died_at = None
        gp.death_cause = None
        await session.commit()
        await services.audit.log(db_user.id, "game_revive", target.id, group.id if group else None, f"game={game.id}")
        await message.answer(f"✨ {esc(display_name(target))} возвращён в игру.")


# ------------------------------------------------------------------ комнаты

@router.message(Command("rooms", "room"))
async def cmd_rooms(message: Message, command: CommandObject, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_STATS) is None:
        return
    rooms_repo = RoomRepository(session)
    args = (command.args or "").strip()
    if args.isdigit():
        room = await rooms_repo.get(int(args))
        if room is None:
            await message.answer("Комната не найдена.")
            return
        lines = [
            f"🏠 <b>КОМНАТА #{room.id}</b> «{esc(room.name)}»",
            f"Статус: {room.status} · 👥 {room.player_count()}/{room.max_players}",
        ]
        for index, membership in enumerate(room.players, start=1):
            lines.append(f"{index}. {esc(display_name(membership.user))} {'🟢' if membership.is_ready else '🟡'}")
        await message.answer("\n".join(lines))
        return
    if group is None:
        await message.answer("В личке укажи ID: /room 123, или используй список в группе.")
        return
    rooms = await rooms_repo.for_group(group.id)
    lines = ["🏠 <b>КОМНАТЫ ГРУППЫ</b>", ""]
    if not rooms:
        lines.append("Открытых комнат нет. Создать: /createroom")
    for room in rooms:
        lines.append(f"• #{room.id} «{esc(room.name)}» — 👥 {room.player_count()}/{room.max_players}")
    await message.answer("\n".join(lines))


@router.message(Command("room_close", "room_kick", "room_force_start", "game_start"))
async def cmd_room_manage(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    permission = Permission.START_GAME if command.command in ("room_force_start", "game_start") else Permission.MANAGE_ROOMS
    if await _require(session, services, message, group, permission) is None:
        return
    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        await message.answer(f"Формат: /{command.command} <room_id> [{'<user>}' if command.command == 'room_kick' else ''}]")
        return
    rooms_repo = RoomRepository(session)
    room = await rooms_repo.get(int(args[0]))
    if room is None:
        await message.answer("Комната не найдена.")
        return
    if group is not None and room.group_id not in (None, group.id):
        await message.answer("⛔️ Комната принадлежит другой группе.")
        return

    if command.command == "room_close":
        message_text = await services.rooms.close_room(room.id, db_user.id)
        await services.audit.log(db_user.id, "room_close", None, group.id if group else None, f"room={room.id}")
        await message.answer(esc(message_text))
        return

    if command.command == "room_kick":
        if len(args) < 2:
            await message.answer("Формат: /room_kick <room_id> <ID|@username>")
            return
        target = await UserLookupService(session).resolve(" ".join(args[1:]))
        if target is None:
            await message.answer("Игрок не найден.")
            return
        _, result = await services.rooms.kick(room.id, db_user.id, target.id)
        await services.audit.log(db_user.id, "room_kick", target.id, group.id if group else None, f"room={room.id}")
        await message.answer(esc(result))
        return

    # room_force_start / game_start
    if room.status != "OPEN":
        await message.answer("Комната не в статусе набора.")
        return

    for membership in room.players:
        if not membership.is_ready:
            membership.is_ready = True
    await session.commit()
    result = await services.games.start_game_from_room(room.id, db_user.id)
    await services.audit.log(
        db_user.id, "room_force_start", None, group.id if group else None,
        f"room={room.id} ok={result.ok}",
    )
    await message.answer(("🎮 " if result.ok else "⚠️ ") + esc(result.message))


@router.message(Command("createroom"))
async def cmd_create_group_room(message: Message, session, group, services, db_user) -> None:
    """Создать комнату с правилами этой группы."""
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    if await _require(session, services, message, group, Permission.START_GAME) is None:
        return
    room, result = await services.groups.create_room_in_group(group.id, db_user.id)
    if room is None:
        await message.answer(f"❌ {esc(result)}")
        return
    await services.audit.log(db_user.id, "create_group_room", None, group.id, f"room={room.id}")
    await message.answer(
        f"✅ {esc(result)}\n\n🏠 Комната #{room.id} «{esc(room.name)}»\n"
        f"👥 {room.player_count()}/{room.max_players}\n\n"
        f"Игроки заходят в боте: «🔎 Найти игру» ➕ По ID → <b>{room.id}</b>\n"
        f"Готовность и старт — как обычно, либо /room_force_start {room.id}"
    )


# ----------------------------------------------------------------- персонал

@router.message(Command("staff"))
async def cmd_staff(message: Message, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_PLAYERS) is None:
        return
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    staff = await services.groups.list_staff(group.id)
    lines = ["👑 <b>АДМИНИСТРАЦИЯ ГРУППЫ</b>", ""]
    if not staff:
        lines.append("Пока только глобальные админы могут управлять группой.")
    from bot.services.permissions import LEVEL_TITLES as T

    for row in staff:
        title = T.get(AdminLevel(row.admin_level), "?")
        lines.append(f"{title} — {esc(display_name(row.user))}")
    await message.answer("\n".join(lines))


@router.message(Command("staff_add", "staff_promote"))
async def cmd_staff_add(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    if await _require(session, services, message, group, Permission.MANAGE_STAFF) is None:
        return
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /staff_add <ID|@username> <уровень 1-4>")
        return
    target = await UserLookupService(session).resolve(args[0])
    if target is None:
        await message.answer("Игрок не найден.")
        return
    if not args[1].isdigit() or not (1 <= int(args[1]) <= 4):
        await message.answer("Уровень: 1 Helper, 2 Moderator, 3 Admin, 4 Senior Admin.")
        return
    if target.telegram_id == message.from_user.id:
        await message.answer("⛔️ Нельзя повышать себя.")
        return
    ok, result = await services.groups.set_staff(
        group.id, message.from_user.id,
        AdminLevel((await _access(session, services, message.from_user.id, group)).level),
        target.id, int(args[1]), db_user.id,
    )
    await message.answer(("✅ " if ok else "❌ ") + esc(result))


@router.message(Command("staff_remove", "staff_demote"))
async def cmd_staff_remove(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    if await _require(session, services, message, group, Permission.MANAGE_STAFF) is None:
        return
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    args = (command.args or "").split()
    target = await _resolve_target(message, session, args[0] if args else None)
    if target is None:
        await message.answer("Формат: /staff_remove <ID|@username>")
        return
    access = await _access(session, services, message.from_user.id, group)
    ok, result = await services.groups.remove_staff(
        group.id, access.level, target.id, db_user.id
    )
    await message.answer(("✅ " if ok else "❌ ") + esc(result))


@router.message(Command("staff_info"))
async def cmd_staff_info(message: Message, command: CommandObject, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.VIEW_PLAYERS) is None:
        return
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    args = (command.args or "").strip()
    target = await _resolve_target(message, session, args or None)
    if target is None:
        await message.answer("Формат: /staff_info <ID|@username>")
        return
    row = await services.groups.get_staff_member(group.id, target.id)
    from bot.services.permissions import LEVEL_TITLES as T

    title = T.get(AdminLevel(row.admin_level), "👤 Player") if row else "👤 Player"
    await message.answer(f"{esc(display_name(target))}: {title}")


# --------------------------------------------------------------- /settings

@router.message(Command("settings"))
async def cmd_settings(message: Message, session, group, services) -> None:
    if await _require(session, services, message, group, Permission.MANAGE_SETTINGS) is None:
        return
    if group is None:
        await message.answer("⚙️ Настройки группы доступны только внутри группы.")
        return
    settings = await services.groups.get_settings(group.id)
    await message.answer(_settings_text(group, settings), reply_markup=_settings_kb())


def _settings_text(group, s) -> str:
    def onoff(value: bool) -> str:
        return "✅" if value else "⛔"

    return "\n".join([
        f"⚙️ <b>НАСТРОЙКИ ГРУППЫ</b>\n<i>{esc(group.title or '')}</i>",
        "",
        f"👥 Игроки: {s.min_players}–{s.max_players}",
        f"⏱ Таймеры: 🌙{s.night_seconds}с ☀️{s.day_seconds}с 🗳{s.vote_seconds}с (обсуждение {s.discussion_seconds}с)",
        f"🎭 Роли: мафия ×{s.mafia_count}, маньяк {onoff(s.allow_maniac)}",
        f"🗳 Ничья: {s.tie_rule} · 💀 раскрытие ролей: {onoff(s.role_reveal_on_death)}",
        "",
        f"⭐ Рейтинг: глобальный {onoff(s.global_rating_enabled)}, локальный {onoff(s.local_rating_enabled)} (общий {onoff(s.rating_enabled)})",
        f"✨ XP: глобальный {onoff(s.global_xp_enabled)}, локальный {onoff(s.local_xp_enabled)} (общий {onoff(s.xp_enabled)})",
        f"🧪 Debug: {onoff(s.debug_enabled)}",
    ])


def _settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Игроки", callback_data=SettingCB(action="players").pack())],
        [InlineKeyboardButton(text="⏱ Таймеры", callback_data=SettingCB(action="timers").pack())],
        [InlineKeyboardButton(text="🎭 Роли", callback_data=SettingCB(action="roles").pack())],
        [InlineKeyboardButton(text="🗳 Голосование", callback_data=SettingCB(action="voting").pack())],
        [InlineKeyboardButton(text="⭐ Рейтинг и XP", callback_data=SettingCB(action="progression").pack())],
        [InlineKeyboardButton(text="🔧 Дополнительно", callback_data=SettingCB(action="extra").pack())],
    ])


def _section_kb(section: str) -> InlineKeyboardMarkup:
    """Секции настроек: числовые кнопки ± и переключатели."""
    def num(label: str, key: str, step: int) -> list[InlineKeyboardButton]:
        minus = f"{key}-minus" if step == 1 else f"{key}-minus{step}"
        plus = f"{key}-plus" if step == 1 else f"{key}-plus{step}"
        return [
            InlineKeyboardButton(text=f"➖{step if step > 1 else ''}", callback_data=SettingCB(action="set", value=minus).pack()),
            InlineKeyboardButton(text=label, callback_data=SettingCB(action=section).pack()),
            InlineKeyboardButton(text=f"➕{step if step > 1 else ''}", callback_data=SettingCB(action="set", value=plus).pack()),
        ]

    def toggle(label: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=SettingCB(action="set", value=key).pack())

    rows: list[list[InlineKeyboardButton]] = []
    if section == "players":
        rows.append(num("👥 Мин. игроков", "minp", 1))
        rows.append(num("👥 Макс. игроков", "maxp", 1))
    elif section == "timers":
        rows.append(num("🌙 Ночь", "night", 30))
        rows.append(num("☀️ День", "day", 30))
        rows.append(num("💬 Обсуждение", "disc", 30))
        rows.append(num("🗳 Голосование", "vote", 30))
    elif section == "roles":
        rows.append(num("🔴 Мафия", "mafia", 1))
        rows.append([toggle("🔪 Маньяк вкл/выкл", "maniac")])
    elif section == "voting":
        rows.append([toggle("⚖️ Ничья: revote/no_death", "tie")])
        rows.append([toggle("💀 Раскрытие ролей", "reveal")])
    elif section == "progression":
        rows.append([toggle("⭐ Рейтинг (общий)", "rating_on")])
        rows.append([toggle("🌐 Глобальный рейтинг", "grating")])
        rows.append([toggle("🏠 Локальный рейтинг", "lrating")])
        rows.append([toggle("✨ XP (общий)", "xp_on")])
        rows.append([toggle("🌐 Глобальный XP", "gxp")])
        rows.append([toggle("🏠 Локальный XP", "lxp")])
    elif section == "extra":
        rows.append([toggle("🧪 Debug в группе", "debug")])
    rows.append([InlineKeyboardButton(text="⬅️ К настройкам", callback_data=SettingCB(action="menu").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(SettingCB.filter())
async def cb_settings(callback: CallbackQuery, callback_data: SettingCB, session, group, services, db_user) -> None:
    access = await _access(session, services, callback.from_user.id, group)
    if Permission.MANAGE_SETTINGS not in access.permissions:
        await callback.answer("⛔️ Нет права MANAGE_SETTINGS", show_alert=True)
        return
    if group is None:
        await callback.answer("Только в группе", show_alert=True)
        return
    action, value = callback_data.action, callback_data.value

    if action == "menu":
        await callback.answer()
        settings = await services.groups.get_settings(group.id)
        await edit_or_answer_safe(callback, _settings_text(group, settings), _settings_kb())
        return

    if action in ("players", "timers", "roles", "voting", "progression", "extra"):
        await callback.answer()
        settings = await services.groups.get_settings(group.id)
        await edit_or_answer_safe(
            callback, _settings_text(group, settings), _section_kb(action)
        )
        return

    if action != "set":
        await callback.answer()
        return
    await callback.answer("Сохранено")

    settings = await services.groups.get_settings(group.id)
    toggles = {
        "tie": ("tie_rule", ("revote", "no_death")),
        "reveal": ("role_reveal_on_death", None),
        "maniac": ("allow_maniac", None),
        "xp_on": ("xp_enabled", None),
        "rating_on": ("rating_enabled", None),
        "gxp": ("global_xp_enabled", None),
        "lxp": ("local_xp_enabled", None),
        "grating": ("global_rating_enabled", None),
        "lrating": ("local_rating_enabled", None),
        "debug": ("debug_enabled", None),
    }
    numbers = {
        "minp": ("min_players", 1, 20, 1),
        "maxp": ("max_players", 4, 20, 1),
        "night": ("night_seconds", 30, 600, 30),
        "day": ("day_seconds", 30, 600, 30),
        "disc": ("discussion_seconds", 30, 600, 30),
        "vote": ("vote_seconds", 30, 600, 30),
        "mafia": ("mafia_count", 1, 4, 1),
    }

    changed_field = None
    if value in toggles:
        field, pair = toggles[value]

        def mutate(s, field=field, pair=pair):
            if pair:
                setattr(s, field, pair[1] if getattr(s, field) == pair[0] else pair[0])
            else:
                setattr(s, field, not getattr(s, field))

        settings = await services.groups.update_settings(group.id, mutate)
        changed_field = f"{field}={getattr(settings, field)}"
    else:
        key, _, direction = value.partition("-")
        if key in numbers and direction:
            field, min_value, max_value, step = numbers[key]
            if direction.startswith("minus"):
                step = -int(direction[5:] or step)
            else:
                step = int(direction[4:] or step)

            def mutate(s, field=field, min_value=min_value, max_value=max_value, step=step):
                setattr(s, field, max(min_value, min(max_value, getattr(s, field) + step)))

            settings = await services.groups.update_settings(group.id, mutate)
            if field == "day_seconds":
                settings = await services.groups.update_settings(
                    group.id, lambda s: setattr(s, "discussion_seconds", s.day_seconds)
                )
            changed_field = f"{field}={getattr(settings, field)}"

    if changed_field:
        await services.audit.log(db_user.id, "group_setting", None, group.id, changed_field)
    await edit_or_answer_safe(callback, _settings_text(group, settings), _settings_kb())


async def edit_or_answer_safe(callback: CallbackQuery, text: str, keyboard=None) -> None:
    from bot.utils.telegram import edit_or_answer

    await edit_or_answer(callback, text, keyboard)


# Быстрые команды-настройки: /set_night_time 60 и т.п.
SETTING_COMMANDS = {
    "set_min_players": ("min_players", 2, 20),
    "set_max_players": ("max_players", 4, 20),
    "set_night_time": ("night_seconds", 30, 600),
    "set_day_time": ("day_seconds", 30, 600),
    "set_vote_time": ("vote_seconds", 30, 600),
}


@router.message(Command(*SETTING_COMMANDS.keys()))
async def cmd_set_number(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    if await _require(session, services, message, group, Permission.MANAGE_SETTINGS) is None:
        return
    if group is None:
        await message.answer("Только в группе.")
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer(f"Формат: /{command.command} <число>")
        return
    field, min_value, max_value = SETTING_COMMANDS[command.command]
    value = max(min_value, min(max_value, int(args)))
    await services.groups.update_settings(group.id, lambda s: setattr(s, field, value))
    if field == "day_seconds":
        await services.groups.update_settings(group.id, lambda s: setattr(s, "discussion_seconds", value))
    await services.audit.log(db_user.id, "group_setting", None, group.id, f"{field}={value}")
    await message.answer(f"✅ {field} = {value}")


@router.message(Command("set_roles"))
async def cmd_set_roles(message: Message, command: CommandObject, session, group, services, db_user) -> None:
    if await _require(session, services, message, group, Permission.MANAGE_SETTINGS) is None:
        return
    if group is None:
        await message.answer("Только в группе.")
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer("Формат: /set_roles mafia 2  (или maniac on|off)")
        return
    field, raw = args[0].lower(), args[1]
    if field == "mafia":
        value = max(1, min(4, int(raw)))
        await services.groups.update_settings(group.id, lambda s: setattr(s, "mafia_count", value))
        await message.answer(f"✅ Мафиев в группе: {value}")
    elif field == "maniac" and raw in ("on", "off"):
        value = raw == "on"
        await services.groups.update_settings(group.id, lambda s: setattr(s, "allow_maniac", value))
        await message.answer(f"✅ Маньяк: {'включён' if value else 'выключен'}")
    else:
        await message.answer("Поддерживается: mafia <1-4>, maniac on|off")
        return
    await services.audit.log(db_user.id, "group_setting", None, group.id, f"{field}={raw}")


# --------------------------------------------------------------- массовые

@router.message(Command("broadcast", "announce"))
async def cmd_broadcast(message: Message, session, group, services, state) -> None:
    from bot.states import AdminStates

    if await _require(session, services, message, group, Permission.BROADCAST) is None:
        return
    await state.set_state(AdminStates.broadcast_input)
    await message.answer("📣 Пришли текст рассылки (HTML разрешён). /cancel — отмена.")


# ---------------------------------------------------------------- система

@router.message(Command("botstats"))
async def cmd_botstats(message: Message, session, services, group) -> None:
    if await _require(session, services, message, group, Permission.VIEW_STATS) is None:
        return
    from bot.database.repositories.games import GameRepository
    from bot.database.repositories.groups import GroupRepository
    from bot.database.repositories.users import UserRepository

    users = await UserRepository(session).count_all()
    games_repo = GameRepository(session)
    active = await games_repo.count_active()
    finished = await games_repo.count_finished()
    groups_count = await GroupRepository(session).count_all()
    await message.answer("\n".join([
        "📊 <b>СТАТИСТИКА БОТА</b>",
        "",
        f"👤 Пользователей: {users}",
        f"🏠 Групп: {groups_count}",
        f"🎮 Активных игр: {active}",
        f"🏁 Завершённых: {finished}",
    ]))


@router.message(Command("logs"))
async def cmd_logs(message: Message, session, services, group) -> None:
    if await _require(session, services, message, group, Permission.BROADCAST) is None:
        return
    from bot.config import get_settings
    from bot.utils.logging import read_log_tail

    tail = read_log_tail(get_settings().log_file, 40)
    await message.answer(f"<code>{esc(tail[-3500:])}</code>")


@router.message(Command("reload"))
async def cmd_reload(message: Message, services, group) -> None:
    from bot.config import get_settings

    settings = get_settings()
    if not (settings.is_owner(message.from_user.id) or settings.is_admin(message.from_user.id)):
        await message.answer("⛔️ Только глобальная администрация.")
        return
    if services.maintenance is not None:
        services.maintenance.invalidate()
    await message.answer("♻️ Кэш настроек сброшен.")


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message, session, services, group) -> None:
    from bot.database.repositories.settings import AppSettingRepository

    access = await _access(session, services, message.from_user.id, group)
    if Permission.MANAGE_GLOBAL_SETTINGS not in access.permissions:
        await _deny(message, Permission.MANAGE_GLOBAL_SETTINGS)
        return
    repo = AppSettingRepository(session)
    stored = await repo.get_global()
    enabled = not bool(stored.get("maintenance", False))
    stored["maintenance"] = enabled
    await repo.set_global(stored)
    await session.commit()
    if services.maintenance is not None:
        services.maintenance.invalidate()
    await message.answer(f"🛠 Режим обслуживания: {'ВКЛ' if enabled else 'ВЫКЛ'}")


# ------------------------------------------------------ справочник Owner

_DEBUG_HELP_TEXT = """👑 <b>СПРАВОЧНИК ВЛАДЕЛЬЦА</b> (уровень 5)

<b>ДИАГНОСТИКА ПРАВ</b>
{diagnostics}

<b>УРОВНИ АДМИНИСТРАЦИИ</b>
0 👤 Player — обычный игрок
1 🛟 Helper — просмотр, warn
2 🔨 Moderator — + mute / kick / ban
3 ⚙️ Admin — + комнаты, старт/стоп игры, DEBUG
4 🎖 Senior Admin — + настройки, штат, broadcast
5 👑 Owner — всё (OWNER_IDS в .env)

<b>👤 ИГРОК</b> (все, везде)
/play — меню · /profile — профиль 🌐+🏠 · /top — рейтинги
/rules — правила · /help — помощь · /cancel — отмена

<code>Профиль/рейтинг в группе показывают и локальную 🏠 статистику.</code>

<b>📈 ПРОФИЛИ И СТАТИСТИКА</b>
/player &lt;ID|@username|reply&gt; — {p1} VIEW_PROFILE
/player_stats — аналог · /players — VIEW_PLAYERS
/stats — своя статистика · /group_stats · /global_stats
/top /top_rating /top_wins /top_levels — топы 🌐/🏠 с пагинацией

<b>🔨 МОДЕРАЦИЯ</b> (в группе; цель — ID/@username/reply)
/warn /unwarn /warnings — WARN_PLAYER
/mute [мин] /unmute — MUTE_PLAYER (Telegram-мут, 1–1440 мин)
/kick — KICK_PLAYER · /ban /unban — BAN_PLAYER

<b>🎮 ИГРА И КОМНАТЫ</b>
/game — активная игра в группе · /games — список
/game_info ID · /game_players · /game_phase
/game_start — MANAGE_ROOMS · /game_stop /game_cancel — STOP_GAME
/game_kill /game_revive — STOP_GAME
/rooms · /room ID · /room_close · /room_kick · /room_force_start
/createroom — комната с правилами группы — MANAGE_ROOMS

<b>👥 ШТАТ</b> (MANAGE_STAFF; защита: не ≥ своего уровня)
/staff · /staff_add &lt;цель&gt; 1-4 · /staff_remove
/staff_promote · /staff_demote · /staff_info

<b>⚙️ НАСТРОЙКИ ГРУППЫ</b> (MANAGE_SETTINGS, только в группе)
/settings — inline-меню (Игроки/Таймеры/Роли/Голосование/Рейтинг/XP/Доп.)
/set_min_players /set_max_players · /set_night_time /set_day_time
/set_vote_time · /set_roles mafia N|maniac on|off

<b>📣 МАССОВЫЕ И СИСТЕМА</b>
/broadcast /announce — BROADCAST (рассылка)
/botstats — VIEW_STATS · /logs — BROADCAST
/reload — OWNER_IDS+ADMIN_IDS · /maintenance — MANAGE_GLOBAL_SETTINGS

<b>🧪 DEBUG MODE</b> (USE_DEBUG: Admin+ при debug_enabled группы; Owner — всегда)
/testgame [4-8|fast] — тест-игра с ботами
/testgame fast — таймеры по 5 сек
/debug — статус · /debug_game ID · /debug_state
/debug_phase · /debug_finish_phase

<code>Статистика тест-игр меняется только при
DEBUG_AFFECTS_GLOBAL/LOCAL_STATS=true в .env (по умолчанию выключены).</code>

<b>👑 ТОЛЬКО ВЛАДЕЛЬЦУ</b>
/debug_help — этот справочник
/admin — админ-панель (OWNER_IDS + ADMIN_IDS)"""


@router.message(Command("debug_help"))
async def cmd_debug_help(message: Message, session, services, group) -> None:
    """Справочник всех команд + диагностика прав. Доступно только уровню 5."""
    access = await _access(session, services, message.from_user.id, group)
    if access.level < AdminLevel.OWNER:
        await message.answer(
            "👑 Команда доступна только глобальному Owner (OWNER_IDS в .env).\n"
            f"Твой текущий уровень: {access.title}"
        )
        return

    from bot.config import get_settings

    env_settings = get_settings()
    tg_id = message.from_user.id
    owners = env_settings.owner_id_list()
    admins = env_settings.admin_id_list()
    diagnostics = "\n".join([
        f"👤 Твой Telegram ID: <code>{tg_id}</code>",
        f"👑 OWNER_IDS распознан: {len(owners)} шт. — "
        + ("<b>ты владелец ✅</b>" if tg_id in owners else "тебя там НЕТ ❌"),
        f"⚙️ ADMIN_IDS распознан: {len(admins)} шт. — "
        + ("ты в списке ✅" if tg_id in admins else "тебя там нет"),
        f"📍 Контекст: {'группа' if group is not None else 'личный чат'} · "
        f"уровень: {access.title}"
        + ("" if tg_id in owners else "\n\n⚠️ Если ID указан в .env, но не распознаётся — "
           "проверь, что он в OWNER_IDS без @ и пробелов, и перезапусти бота "
           "(env читается только при старте)."),
    ])
    text = _DEBUG_HELP_TEXT.format(diagnostics=diagnostics, p1="свой — всегда, чужой —")
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await message.answer(text)
