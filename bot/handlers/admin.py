"""Админ-панель: статистика, игры, комнаты, баны, рассылка, логи, роли, параметры.

Доступ для OWNER_IDS и ADMIN_IDS из .env (проверка на каждом действии):
владелец (OWNER_IDS) проходит всегда, администраторы (ADMIN_IDS) — как и раньше.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.database.repositories.games import GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.keyboards.admin import (
    admin_back_kb,
    admin_confirm_end_kb,
    admin_games_kb,
    admin_panel_kb,
    admin_params_kb,
    admin_roles_kb,
    admin_rooms_kb,
)
from bot.services.app_config import GlobalSettings
from bot.states import AdminStates
from bot.utils.callbacks import AdminCB
from bot.utils.helpers import esc
from bot.utils.logging import read_log_tail
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


def _is_admin(user_id: int) -> bool:
    # Глобальный Owner (OWNER_IDS) имеет право на админ-панель всегда,
    # даже если его ID не указан в ADMIN_IDS.
    settings = get_settings()
    return settings.is_admin(user_id) or settings.is_owner(user_id)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:",
        reply_markup=admin_panel_kb(debug_mode=get_settings().debug_mode),
    )


async def _guard(callback: CallbackQuery) -> bool:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Недостаточно прав", show_alert=True)
        return False
    return True


@router.callback_query(AdminCB.filter(F.action == "panel"))
async def cb_admin_panel(callback: CallbackQuery) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await edit_or_answer(
        callback,
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:",
        admin_panel_kb(debug_mode=get_settings().debug_mode),
    )


@router.callback_query(AdminCB.filter(F.action == "testgame"))
async def cb_admin_testgame(callback: CallbackQuery) -> None:
    """🧪 Тестовая игра: показать меню DEBUG MODE."""
    if not await _guard(callback):
        return
    settings = get_settings()
    if not settings.debug_mode:
        await callback.answer("DEBUG_MODE выключен в .env", show_alert=True)
        return
    from bot.keyboards.testgame import test_players_count_kb

    await callback.answer()
    await edit_or_answer(
        callback,
        "🧪 <b>ТЕСТОВЫЙ РЕЖИМ</b>\n\nВыбери количество участников:\n"
        "(подробности: /testgame)",
        test_players_count_kb(),
    )


@router.callback_query(AdminCB.filter(F.action == "stats"))
async def cb_admin_stats(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    users = await UserRepository(session).count_all()
    games_repo = GameRepository(session)
    active = await games_repo.count_active()
    finished = await games_repo.count_finished()
    day_games = await games_repo.count_active_today()
    rooms_open = await RoomRepository(session).count_open()
    text = "\n".join([
        "📊 <b>СТАТИСТИКА БОТА</b>",
        "",
        f"👤 Пользователей: <b>{users}</b>",
        f"🎮 Активных игр: <b>{active}</b>",
        f"🏁 Завершённых игр: <b>{finished}</b>",
        f"🕒 Игр за 24 часа: <b>{day_games}</b>",
        f"🏠 Открытых комнат: <b>{rooms_open}</b>",
    ])
    await edit_or_answer(callback, text, admin_back_kb())


@router.callback_query(AdminCB.filter(F.action == "games"))
async def cb_admin_games(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    games = await GameRepository(session).active_games()
    if not games:
        await edit_or_answer(callback, "🎮 Активных игр нет.", admin_back_kb())
        return
    lines = ["🎮 <b>АКТИВНЫЕ ИГРЫ</b>", ""]
    for game in games:
        lines.append(f"• #{game.id}: {game.status}, день {game.day_number}")
    await edit_or_answer(callback, "\n".join(lines), admin_games_kb(games))


@router.callback_query(AdminCB.filter(F.action == "endgame"))
async def cb_admin_endgame(callback: CallbackQuery, callback_data: AdminCB, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    game = await GameRepository(session).get(int(callback_data.value))
    if game is None:
        await edit_or_answer(callback, "Игра не найдена.", admin_back_kb())
        return
    await edit_or_answer(
        callback,
        f"🏁 Завершить игру #{game.id} принудительно?\nИгроки получат уведомление, рейтинг не изменится.",
        admin_confirm_end_kb(game.id),
    )


@router.callback_query(AdminCB.filter(F.action == "endgame_yes"))
async def cb_admin_endgame_yes(callback: CallbackQuery, callback_data: AdminCB, services) -> None:
    if not await _guard(callback):
        return
    done = await services.phases.force_end(int(callback_data.value), "Завершено администратором")
    await callback.answer("Завершена" if done else "Игра не активна", show_alert=True)
    await edit_or_answer(callback, "✅ Игра завершена." if done else "Игра не активна.", admin_back_kb())


@router.callback_query(AdminCB.filter(F.action == "rooms"))
async def cb_admin_rooms(callback: CallbackQuery, session) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    rooms = await RoomRepository(session).list_open(20)
    if not rooms:
        await edit_or_answer(callback, "🏠 Активных комнат нет.", admin_back_kb())
        return
    lines = ["🏠 <b>КОМНАТЫ</b>", ""]
    for room in rooms:
        lines.append(
            f"• #{room.id} «{esc(room.name)}» [{room.status}] — 👥 {room.player_count()}/{room.max_players}"
        )
    await edit_or_answer(callback, "\n".join(lines), admin_rooms_kb(rooms))


@router.callback_query(AdminCB.filter(F.action == "closeroom"))
async def cb_admin_close_room(callback: CallbackQuery, callback_data: AdminCB, session) -> None:
    if not await _guard(callback):
        return
    rooms = RoomRepository(session)
    room = await rooms.get(int(callback_data.value))
    if room is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    if room.status == "PLAYING":
        await callback.answer("Нельзя закрыть комнату с активной игрой", show_alert=True)
        return
    room.status = "CLOSED"
    await session.commit()
    await callback.answer("Комната закрыта")
    await cb_admin_rooms(callback, AdminCB(action="rooms"), session)


# ------------------------------------------------------------------- баны

@router.callback_query(AdminCB.filter(F.action == "ban"))
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.ban_input)
    await edit_or_answer(callback, "🚫 Пришли Telegram ID пользователя для бана:")


@router.message(AdminStates.ban_input)
async def process_ban(message: Message, state: FSMContext, session) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз.")
        return
    await state.clear()
    user = await UserRepository(session).set_banned(int(raw), True)
    await session.commit()
    await message.answer(
        "🚫 Забанен." if user else "Пользователь не найден в БД.", reply_markup=admin_back_kb()
    )


@router.callback_query(AdminCB.filter(F.action == "unban"))
async def cb_admin_unban(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.unban_input)
    await edit_or_answer(callback, "✅ Пришли Telegram ID пользователя для разбана:")


@router.message(AdminStates.unban_input)
async def process_unban(message: Message, state: FSMContext, session) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужен числовой Telegram ID. Попробуй ещё раз.")
        return
    await state.clear()
    user = await UserRepository(session).set_banned(int(raw), False)
    await session.commit()
    await message.answer(
        "✅ Разбанен." if user else "Пользователь не найден в БД.", reply_markup=admin_back_kb()
    )


# --------------------------------------------------------------- рассылка

@router.callback_query(AdminCB.filter(F.action == "broadcast"))
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.broadcast_input)
    await edit_or_answer(
        callback,
        "📣 Пришли текст рассылки (HTML разрешён).\n/cancel — отмена.",
    )


@router.message(Command("cancel"), AdminStates.broadcast_input)
@router.message(Command("cancel"), AdminStates.param_input)
async def admin_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_back_kb())


@router.message(AdminStates.broadcast_input)
async def process_broadcast(message: Message, state: FSMContext, session, services) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = message.html_text or (message.text or "")
    await state.clear()
    ids = await UserRepository(session).ids_for_broadcast()
    await message.answer(f"📣 Рассылка {len(ids)} пользователям...")
    ok, failed = await services.notifier.broadcast(ids, text)
    await message.answer(f"📣 Готово: доставлено {ok}, ошибок {failed}.", reply_markup=admin_back_kb())


# ------------------------------------------------------------------- логи

@router.callback_query(AdminCB.filter(F.action == "logs"))
async def cb_admin_logs(callback: CallbackQuery) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    from bot.config import get_settings

    tail = read_log_tail(get_settings().log_file, lines=40)
    await edit_or_answer(callback, f"📜 <b>ЛОГИ (последние строки)</b>\n\n<code>{esc(tail)}</code>", admin_back_kb())


# ------------------------------------------------------------------- роли

@router.callback_query(AdminCB.filter(F.action == "roles"))
async def cb_admin_roles(callback: CallbackQuery, services) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    gs = await services.app_config.get()
    enabled = {r.id for r in gs.enabled_role_objects()}
    await edit_or_answer(
        callback,
        "🎭 <b>УПРАВЛЕНИЕ РОЛЯМИ</b>\n\n✅ — роль доступна в настройках комнат, ⛔ — скрыта.",
        admin_roles_kb(enabled),
    )


@router.callback_query(AdminCB.filter(F.action == "roletoggle"))
async def cb_admin_role_toggle(callback: CallbackQuery, callback_data: AdminCB, services) -> None:
    if not await _guard(callback):
        return
    gs = await services.app_config.get()
    role_id = callback_data.value
    current = {r.id for r in gs.enabled_role_objects()}
    if role_id in current:
        current.discard(role_id)
    else:
        current.add(role_id)
    gs.enabled_roles = sorted(current)
    await services.app_config.save(gs)
    await callback.answer("Обновлено")
    await cb_admin_roles(callback, services)


# ------------------------------------------------------- глобальные параметры

@router.callback_query(AdminCB.filter(F.action == "gparams"))
async def cb_admin_params(callback: CallbackQuery, services) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    gs = await services.app_config.get()
    await edit_or_answer(
        callback,
        "⚙️ <b>ГЛОБАЛЬНЫЕ ПАРАМЕТРЫ</b> (по умолчанию для новых комнат)\n\n"
        f"🌙 Ночь: {gs.night_seconds} сек\n"
        f"☀️ День: {gs.day_seconds} сек\n"
        f"🗳 Голосование: {gs.vote_seconds} сек",
        admin_params_kb(),
    )


@router.callback_query(AdminCB.filter(F.action == "setparam"))
async def cb_admin_setparam(callback: CallbackQuery, callback_data: AdminCB, state: FSMContext) -> None:
    if not await _guard(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.param_input)
    await state.update_data(param=callback_data.value)
    await edit_or_answer(callback, f"Введи новое значение параметра <b>{callback_data.value}</b> (секунды, 30–600):")


@router.message(AdminStates.param_input)
async def process_param(message: Message, state: FSMContext, services) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    param = data.get("param")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (30 <= int(raw) <= 600):
        await message.answer("Нужно число 30–600. Попробуй ещё раз.")
        return
    await state.clear()
    gs: GlobalSettings = await services.app_config.get()
    setattr(gs, param, int(raw))
    await services.app_config.save(gs)
    await message.answer(f"✅ {param} = {raw} сек.", reply_markup=admin_back_kb())


# ============================== OWNER-ONLY: ручная выдача глобальной статистики
# Выдача/изменение рейтингов, XP, уровней и достижений доступна ТОЛЬКО OWNER
# (OWNER_IDS в .env). Проверка серверная — global_level == OWNER из
# PermissionService; ADMIN_IDS (Senior Admin) и групповые админы доступа НЕ имеют:
# ручная выдача могла бы испортить глобальный рейтинг проекта.


def _is_owner(services, telegram_id: int) -> bool:
    from bot.services.permissions import AdminLevel

    return services.permissions.global_level(telegram_id) >= AdminLevel.OWNER


async def _deny_owner(message: Message) -> None:
    await message.answer("⛔️ Только владельцу бота (OWNER_IDS).")


async def _owner_target(session, message: Message, command: CommandObject):
    """Цель: первый аргумент (ID/@username) или ответ на сообщение."""
    from bot.services.lookup import UserLookupService

    args = (command.args or "").split()
    query = args[0] if args else None
    target = await UserLookupService(session).resolve(
        query=query,
        reply_telegram_id=(
            message.reply_to_message.from_user.id
            if message.reply_to_message and message.reply_to_message.from_user
            else None
        ),
    )
    return target, args


async def _audit_owner(session, actor, target, action: str, details: str) -> None:
    from bot.database.repositories.groups import AuditLogRepository

    await AuditLogRepository(session).log(
        actor_id=actor.id, target_id=target.id, group_id=None,
        action=action, details=details[:512],
    )


@router.message(Command(
    "set_rating", "add_rating", "set_wins", "add_wins", "set_xp", "add_xp", "set_level"
))
async def cmd_owner_stats(message: Message, command: CommandObject, session,
                          db_user, services) -> None:
    """OWNER-only: ручное изменение рейтинга/побед/XP/уровня любого игрока."""
    if not _is_owner(services, message.from_user.id):
        await _deny_owner(message)
        return

    from bot.services.progression import DEFAULT_PROGRESSION
    from bot.utils.helpers import display_name

    target, args = await _owner_target(session, message, command)
    if target is None or len(args) < 2 or not args[1].lstrip("-").isdigit():
        await message.answer(
            "Формат: <code>/set_rating|add_rating|set_wins|add_wins|"
            "set_xp|add_xp|set_level &lt;ID|@username&gt; &lt;число&gt;</code>"
        )
        return
    value = int(args[1])
    name = command.command

    if name in ("set_rating", "add_rating"):
        target.rating = value if name == "set_rating" else max(0, target.rating + value)
        new_value, field = target.rating, "рейтинг"
        details = f"rating={target.rating}"
    elif name in ("set_wins", "add_wins"):
        target.wins = value if name == "set_wins" else max(0, target.wins + value)
        new_value, field = target.wins, "победы"
        details = f"wins={target.wins}"
    elif name in ("set_xp", "add_xp"):
        target.xp = value if name == "set_xp" else max(0, target.xp + value)
        target.level = DEFAULT_PROGRESSION.level_for_xp(target.xp)
        new_value, field = target.xp, "XP"
        details = f"xp={target.xp} level={target.level}"
    else:  # set_level
        if not 1 <= value <= 99:
            await message.answer("Уровень должен быть от 1 до 99.")
            return
        target.level = value
        target.xp = DEFAULT_PROGRESSION.threshold(value)  # XP синхронизируется с уровнем
        new_value, field = target.level, "уровень"
        details = f"level={target.level} xp={target.xp}"

    await _audit_owner(session, db_user, target, name, details)
    await session.commit()

    from bot.database.repositories.users import UserRepository

    users = UserRepository(session)
    rank = {
        "rating": await users.rank_by_rating(target.rating),
        "wins": await users.rank_by_wins(target.wins),
        "level": await users.rank_by_level(target.level, target.xp),
    }
    await message.answer(
        f"✅ {esc(display_name(target))}: {field} = <b>{new_value}</b>\n"
        f"⭐ Общий: <b>{target.rating}</b> (#{rank['rating']}) · "
        f"🏆 Побед: <b>{target.wins}</b> (#{rank['wins']}) · "
        f"📈 Уровень: <b>{target.level}</b> (#{rank['level']}) · ✨ XP: <b>{target.xp}</b>"
    )


@router.message(Command("achievement_grant", "achievement_remove"))
async def cmd_owner_achievements(message: Message, command: CommandObject,
                                 session, db_user, services) -> None:
    """OWNER-only: ручная выдача/снятие достижения (для проверки профиля)."""
    if not _is_owner(services, message.from_user.id):
        await _deny_owner(message)
        return

    from bot.database.repositories.social import UserAchievementRepository
    from bot.services import achievements as ach
    from bot.services import rewards as rw
    from bot.utils.helpers import display_name

    target, args = await _owner_target(session, message, command)
    if target is None or len(args) < 2:
        ids = ", ".join(a.id for a in ach.all_achievements())
        await message.answer(
            f"Формат: <code>/achievement_grant|achievement_remove &lt;ID|@username&gt; &lt;achievement_id&gt;</code>\n"
            f"Доступно: <code>{ids}</code>"
        )
        return
    aid = args[1].strip()
    definition = ach.get_achievement(aid)
    if definition is None:
        await message.answer(f"Неизвестное достижение: <code>{esc(aid)}</code>.")
        return

    if command.command == "achievement_grant":
        # тот же путь, что и автоматическая выдача (титулы открываются сами)
        newly = await rw.award_achievements(session, {target.id: {aid}})
        await _audit_owner(session, db_user, target, "achievement_grant", f"aid={aid}")
        await session.commit()
        if newly:
            from bot.services import titles as ttl

            title = ttl.get_title(ttl.TITLE_UNLOCKS.get(aid))
            extra = f" Открыт титул: {title.name}." if title else ""
            await message.answer(
                f"🏅 {definition.emoji} «{definition.name}» выдан: "
                f"{esc(display_name(target))}.{extra}"
            )
        else:
            await message.answer("Это достижение у игрока уже есть.")
    else:
        removed = await UserAchievementRepository(session).remove(target.id, aid)
        if removed:
            # снимаем и титул, открытый этим достижением (если он из достижения)
            from bot.services import titles as ttl

            title_id = ttl.TITLE_UNLOCKS.get(aid)
            if title_id:
                from bot.database.repositories.social import UserTitleRepository

                await UserTitleRepository(session).remove(
                    target.id, title_id, source="achievement"
                )
                if target.active_title == title_id:
                    target.active_title = None
            await _audit_owner(session, db_user, target, "achievement_remove", f"aid={aid}")
        await session.commit()
        if removed:
            await message.answer(
                f"🏅 «{definition.name}» снят с {esc(display_name(target))}."
            )
        else:
            await message.answer("Такого достижения у игрока нет.")


@router.message(Command("level_info"))
async def cmd_level_info(message: Message) -> None:
    """Таблица уровней (сколько XP нужно на каждый уровень)."""
    from bot.services.progression import DEFAULT_PROGRESSION as prog

    lines = ["📈 <b>ТАБЛИЦА УРОВНЕЙ</b>", ""]
    for lvl in range(2, 26):
        need = prog.threshold(lvl) - prog.threshold(lvl - 1)
        lines.append(f"Ур. {lvl} — {prog.threshold(lvl)} XP (+{need} за уровень)")
    lines.append("")
    lines.append("25+ — дальше с шагом +50 XP за каждый уровень.")
    await message.answer("\n".join(lines))
