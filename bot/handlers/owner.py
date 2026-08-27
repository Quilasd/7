"""OWNER-ПАНЕЛЬ: /owner — кнопочное меню владельца (исключительно OWNER_IDS).

Переиспользует существующие системы и НЕ дублирует их:
- UserLookupService — поиск игроков (ID/@username/reply);
- ProgressionService — XP → уровень (та же формула, что в играх);
- UserRepository / GroupPlayerRepository — рейтинги (global/local);
- UserAchievementRepository + rewards.award_achievements — достижения и титулы;
- RewardService / EventRewardRepository — ивентовые награды;
- TestGameManager — тестовая игра с ботами (включая fast-режим);
- AppSettingRepository / read_log_tail / AdminStates.broadcast_input — система;
- AuditLogRepository — журнал действий.

Права: КАЖДЫЙ handler (команда и callback) проверяет OWNER на сервере через
PermissionService.global_level() == OWNER. /admin не затрагивается.

FSM (OwnerStates) — только там, где нужен ввод текста: поиск игрока, ввод
числа, создание награды. Подтверждения опасных действий — stateless: всё
закодировано в callback_data кнопки ✅.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.permissions import AdminLevel
from bot.states import OwnerStates
from bot.utils.callbacks import OwnerCB
from bot.utils.helpers import esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router(name="owner")


# ------------------------------------------------------------------ права

def _is_owner(services, telegram_id: int) -> bool:
    return services.permissions.global_level(telegram_id) >= AdminLevel.OWNER


async def _guard_cb(callback: CallbackQuery, services) -> bool:
    """Серверная проверка OWNER на каждом callback (права могли отозвать)."""
    if not _is_owner(services, callback.from_user.id):
        await callback.answer("⛔️ Только владельцу", show_alert=True)
        return False
    return True


def _deny_msg(message: Message) -> None:
    message.answers.append("⛔️ Панель доступна только владельцу бота (OWNER_IDS).")


# ------------------------------------------------------------------ клавиатуры

def _btn(text: str, action: str, value: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=OwnerCB(action=action, value=value).pack())


def _rows(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def _main_kb() -> InlineKeyboardMarkup:
    return _rows(
        [_btn("📊 Статистика", "stats"), _btn("👥 Игроки", "players")],
        [_btn("🏆 Рейтинги", "ratings"), _btn("✨ XP и уровни", "xp")],
        [_btn("🏅 Достижения", "achievements"), _btn("🎖 Титулы", "titles")],
        [_btn("🎪 Ивенты", "rewards"), _btn("🎮 Тестовая игра", "testgame")],
        [_btn("🧪 Debug", "debug"), _btn("⚙️ Система", "system")],
        [_btn("🔨 Администрация", "staff")],
        [_btn("❌ Закрыть", "close")],
    )


def _back_kb(action: str = "main") -> InlineKeyboardMarkup:
    return _rows([_btn("◀️ Назад", action), _btn("❌ Закрыть", "close")])


def _cancel_kb(back: str = "main") -> InlineKeyboardMarkup:
    return _rows([_btn("❌ Отмена", "cancel"), _btn("◀️ В меню", "main")])


# ------------------------------------------------------------------ экраны

async def _screen_stats(session) -> str:
    from sqlalchemy import func, select

    from bot.database.models import (
        Friendship,
        UserAchievement,
        UserEventReward,
        UserTitle,
    )
    from bot.database.repositories.games import GameRepository
    from bot.database.repositories.groups import GroupRepository
    from bot.database.repositories.users import UserRepository
    from bot.services.achievements import total_achievements

    async def _count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        if where:
            stmt = stmt.where(*where)
        return int((await session.execute(stmt)).scalar_one())

    users = await UserRepository(session).count_all()
    games = GameRepository(session)
    groups_count = await GroupRepository(session).count_all()
    lines = [
        "📊 <b>СТАТИСТИКА БОТА</b>",
        "",
        f"👤 Игроков: <b>{users}</b> · 🏠 Групп: <b>{groups_count}</b>",
        f"🎮 Активных игр: <b>{await games.count_active()}</b>"
        f" · сегодня: <b>{await games.count_active_today()}</b>",
        f"🏁 Завершённых игр: <b>{await games.count_finished()}</b>",
        "",
        f"🏅 Выдано достижений: <b>{await _count(UserAchievement)}</b>"
        f" (всего видов: {total_achievements()})",
        f"🎖 Открытых титулов: <b>{await _count(UserTitle)}</b>",
        f"🎪 Выданных наград: <b>{await _count(UserEventReward)}</b>",
        f"👥 Дружб: <b>{await _count(Friendship)}</b>",
    ]
    return "\n".join(lines)


def _top_lines(users: list, metric: str) -> list[str]:
    titles = {"rating": "⭐", "wins": "🏆", "level": "📈"}
    lines = []
    for i, u in enumerate(users, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        value = {"rating": u.rating, "wins": u.wins, "level": f"{u.level} ({u.xp} XP)"}[metric]
        lines.append(f"{medal} {esc(u.username or u.display_name or u.telegram_id)} — "
                     f"{titles[metric]} {value}")
    return lines or ["Пока пусто."]


async def _screen_ratings(session, scope: str, metric: str, group=None) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.groups import GroupPlayerRepository
    from bot.database.repositories.users import UserRepository

    title = {"rating": "⭐ ОБЩИЙ РЕЙТИНГ", "wins": "🏆 РЕЙТИНГ ПОБЕД",
             "level": "📈 РЕЙТИНГ УРОВНЕЙ"}[metric]
    if scope == "local" and group is None:
        scope = "global"
    lines = [f"🏆 <b>{title}</b> · {'🏠 ' + esc(group.title) if scope == 'local' else '🌐 Глобальный'}", ""]
    if scope == "local":
        players = await GroupPlayerRepository(session).top(group.id, metric, limit=1000)
        lines += _top_lines(players, metric)
    else:
        repo = UserRepository(session)
        fetch = {"rating": repo.top_by_rating, "wins": repo.top_by_wins,
                 "level": repo.top_by_level}[metric]
        users = await fetch(10)
        lines += _top_lines(users, metric)

    kb = _rows(
        [_btn("⭐ Общий рейтинг", "ratings", f"{scope}.rating"),
         _btn("🏆 Победы", "ratings", f"{scope}.wins"),
         _btn("📈 Уровень", "ratings", f"{scope}.level")],
        [_btn("🌐 Global", "ratings", f"global.{metric}"),
         _btn("🏠 Local", "ratings", f"local.{metric}")],
        [_btn("➕ Добавить общий", "act", "rating_add"),
         _btn("✏️ Установить общий", "act", "rating_set")],
        [_btn("➕ Добавить победы", "act", "wins_add"),
         _btn("✏️ Установить победы", "act", "wins_set")],
        [_btn("✨ Уровневый → XP", "xp"), _btn("👤 Изменить игроку", "players")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


async def _screen_player(session, services, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.social import UserAchievementRepository
    from bot.database.repositories.users import UserRepository
    from bot.services.achievements import total_achievements

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    streak = int(getattr(user, "win_streak", 0) or 0)
    ach = await UserAchievementRepository(session).count(user.id)
    from bot.utils.helpers import xp_progress_lines

    lines = [
        f"👤 <b>{esc(user.display_name or user.username or user.telegram_id)}</b>",
        f"ID: <code>{user.telegram_id}</code>"
        + (f" · @{esc(user.username)}" if user.username else ""),
        "",
        f"⭐ Общий рейтинг: <b>{user.rating}</b>",
        *xp_progress_lines(user.xp),
        f"🏆 Победы: <b>{user.wins}</b> · 💀 Поражений: <b>{user.losses}</b>",
        f"📈 Уровень: <b>{user.level}</b>",
        f"🔥 Серия: <b>{streak}</b> · 🏅 Достижения: <b>{ach}/{total_achievements()}</b>",
    ]
    kb = _rows(
        [_btn("🏆 Рейтинг", "player", f"ratings.{user.id}"),
         _btn("✨ XP/Уровень", "player", f"xp.{user.id}")],
        [_btn("🏅 Достижения", "player", f"ach.{user.id}"),
         _btn("🎖 Титулы", "player", f"titles.{user.id}")],
        [_btn("🎪 Награды", "player", f"rewards.{user.id}")],
        [_btn("◀️ Назад", "players"), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


async def _screen_player_ratings(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.users import UserRepository

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    name = esc(user.display_name or user.username or user.telegram_id)
    lines = [
        f"🏆 <b>РЕЙТИНГИ</b> · {name}", "",
        f"⭐ Общий: <b>{user.rating}</b> · 🏆 Победы: <b>{user.wins}</b>",
    ]
    kb = _rows(
        [_btn("➕ Общий", "act", f"rating_add.{user.id}"),
         _btn("✏️ Общий", "act", f"rating_set.{user.id}")],
        [_btn("➕ Победы", "act", f"wins_add.{user.id}"),
         _btn("✏️ Победы", "act", f"wins_set.{user.id}")],
        [_btn("◀️ Назад", "player", str(user.id)), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


async def _screen_player_xp(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.users import UserRepository
    from bot.services.progression import DEFAULT_PROGRESSION as prog

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    name = esc(user.display_name or user.username or user.telegram_id)
    from bot.utils.helpers import xp_progress_lines

    lines = [
        f"✨ <b>XP И УРОВЕНЬ</b> · {name}", "",
        f"📈 Уровень: <b>{user.level}</b>",
        *xp_progress_lines(user.xp),
        f"<i>Общий XP аккаунта: {user.xp}</i>",
    ]
    kb = _rows(
        [_btn("➕ XP", "act", f"xp_add.{user.id}"),
         _btn("✏️ XP", "act", f"xp_set.{user.id}")],
        [_btn("📈 Уровень", "act", f"level_set.{user.id}"),
         _btn("📊 Таблица", "leveltable")],
        [_btn("◀️ Назад", "player", str(user.id)), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


LEVEL_TABLE_PAGE_SIZE = 10
LEVEL_TABLE_MAX = 50  # показываем первые 50 уровней; дальше формула та же


def _screen_leveltable(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Таблица уровней по 10 на экран, значения из ProgressionService."""
    from bot.services.progression import DEFAULT_PROGRESSION as prog

    pages = (LEVEL_TABLE_MAX + LEVEL_TABLE_PAGE_SIZE - 1) // LEVEL_TABLE_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    lo = page * LEVEL_TABLE_PAGE_SIZE + 1
    hi = min(lo + LEVEL_TABLE_PAGE_SIZE - 1, LEVEL_TABLE_MAX)

    lines = ["📊 <b>ТАБЛИЦА УРОВНЕЙ</b>", "",
             "<i>Уровень · XP внутри уровня · суммарный XP</i>", ""]
    for lvl in range(lo, hi + 1):
        lines.append(
            f"Уровень <b>{lvl}</b> → {prog.requirement(lvl)} XP "
            f"(всего {prog.threshold(lvl + 1) if lvl < LEVEL_TABLE_MAX else '…'})"
        )
    lines.append("")
    lines.append("Дальше требования продолжают расти (+20 XP за уровень к приросту).")

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn(f"◀️ {page * 10 - 9}–{page * 10}", "leveltable", str(page - 1)))
    nav.append(_btn(f"{lo}–{hi} / {LEVEL_TABLE_MAX}", "noop"))
    if page < pages - 1:
        nav.append(_btn(f"{hi + 1}–{min(hi + 10, LEVEL_TABLE_MAX)} ▶️", "leveltable", str(page + 1)))
    rows = [nav, [_btn("◀️ Назад", "xp"), _btn("❌ Закрыть", "close")]]
    return "\n".join(lines), _rows(*rows)


async def _screen_achievements(session) -> tuple[str, InlineKeyboardMarkup]:
    from sqlalchemy import func, select

    from bot.database.models import UserAchievement
    from bot.services import achievements as ach

    counts: dict[str, int] = {}
    rows = await session.execute(
        select(UserAchievement.achievement_id, func.count())
        .group_by(UserAchievement.achievement_id)
    )
    for aid, cnt in rows.all():
        counts[str(aid)] = int(cnt)
    lines = ["🏅 <b>ДОСТИЖЕНИЯ</b>", ""]
    for a in ach.all_achievements():
        mark = "🔒" if a.hidden else "•"
        lines.append(f"{mark} {a.emoji} <b>{a.name}</b> <code>{a.id}</code> — {a.description}"
                     f" · у {counts.get(a.id, 0)} игр.")
    kb = _rows(
        [_btn("👤 Игрок", "players"), _btn("➕ Выдать", "act", "ach_grant")],
        [_btn("❌ Снять", "act", "ach_revoke")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


async def _screen_player_ach(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.social import UserAchievementRepository
    from bot.database.repositories.users import UserRepository
    from bot.services import achievements as ach

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    earned = await UserAchievementRepository(session).ids_of(user.id)
    name = esc(user.display_name or user.username or user.telegram_id)
    lines = [f"🏅 <b>ДОСТИЖЕНИЯ</b> · {name} — {len(earned)}/{ach.total_achievements()}", ""]
    for a in ach.all_achievements():
        has = a.id in earned
        lines.append(("✅" if has else "⬜") + f" {a.emoji} {a.name}"
                     + ("" if not a.hidden or has else " 🔒"))
    # по кнопке на каждое достижение: выдать/снять
    rows: list[list[InlineKeyboardButton]] = []
    for a in ach.all_achievements():
        verb = "❌" if a.id in earned else "➕"
        rows.append([_btn(f"{verb} {a.name}", "item", f"ach.{user.id}.{a.id}")])
    rows.append([_btn("◀️ Назад", "player", str(user.id)), _btn("❌ Закрыть", "close")])
    return "\n".join(lines), _rows(*rows)


async def _screen_titles(session) -> tuple[str, InlineKeyboardMarkup]:
    from bot.services import titles as ttl

    unlocks = {v: k for k, v in ttl.TITLE_UNLOCKS.items()}
    lines = ["🎖 <b>ТИТУЛЫ</b>", ""]
    for t in ttl._ALL_TITLES:  # noqa: SLF001
        how = f"за <code>{unlocks[t.id]}</code>" if t.id in unlocks else "админ/ивент"
        lines.append(f"{t.emoji} <b>{t.name}</b> <code>{t.id}</code> — {how}")
    kb = _rows(
        [_btn("👤 Игрок", "players"), _btn("➕ Выдать", "act", "title_grant")],
        [_btn("❌ Снять", "act", "title_remove")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


async def _screen_player_titles(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.social import UserTitleRepository
    from bot.database.repositories.users import UserRepository
    from bot.services import titles as ttl

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    unlocked = set(await UserTitleRepository(session).ids_of(user.id))
    active = user.active_title
    name = esc(user.display_name or user.username or user.telegram_id)
    lines = [f"🎖 <b>ТИТУЛЫ</b> · {name}", ""]
    for t in ttl._ALL_TITLES:  # noqa: SLF001
        mark = "✅" if t.id in unlocked else "⬜"
        act = " · <b>АКТИВНЫЙ</b>" if t.id == active else ""
        lines.append(f"{mark} {t.emoji} {t.name}{act}")
    rows: list[list[InlineKeyboardButton]] = []
    for t in ttl._ALL_TITLES:  # noqa: SLF001
        verb = "❌" if t.id in unlocked else "➕"
        rows.append([_btn(f"{verb} {t.name}", "item", f"title.{user.id}.{t.id}")])
    rows.append([_btn("◀️ Назад", "player", str(user.id)), _btn("❌ Закрыть", "close")])
    return "\n".join(lines), _rows(*rows)


async def _rewards_text(catalog: list) -> str:
    lines = ["🎪 <b>ИВЕНТОВЫЕ НАГРАДЫ</b>", ""]
    if not catalog:
        lines.append("Каталог пуст. Создай первой кнопкой ниже.")
    for r in catalog:
        exp = f", {r.expires_days} дн." if r.expires_days else ", бессрочно"
        lines.append(f"{r.emoji} <b>{esc(r.name)}</b> <code>{r.code}</code> [{r.kind}{exp}]")
    lines.append("")
    lines.append("<i>Выдача — раздел 👥 Игроки → 🎪 Награды.</i>")
    return "\n".join(lines)


async def _screen_player_rewards(session, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from bot.database.repositories.social import UserEventRewardRepository
    from bot.database.repositories.users import UserRepository
    from bot.utils.helpers import utcnow

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return "Игрок не найден.", _back_kb("players")
    rewards = await UserEventRewardRepository(session).of_user(user.id)
    name = esc(user.display_name or user.username or user.telegram_id)
    lines = [f"🎪 <b>НАГРАДЫ</b> · {name}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if not rewards:
        lines.append("Наград нет.")
    for r in rewards:
        reward = r.reward
        active = r.expires_at is None or r.expires_at > utcnow()
        state = "активна" if active else "истекла (в истории)"
        lines.append(f"{reward.emoji} <b>{esc(reward.name)}</b> <code>{reward.code}</code> — {state}")
        if active:
            rows.append([_btn(f"❌ Забрать {reward.name}", "item", f"reward.{user.id}.{r.id}")])
    rows.append([_btn("🎁 Выдать", "act", f"reward_grant.{user.id}"),
                 _btn("📋 Каталог", "rewards")])
    rows.append([_btn("◀️ Назад", "player", str(user.id)), _btn("❌ Закрыть", "close")])
    return "\n".join(lines), _rows(*rows)


def _screen_testgame(services) -> tuple[str, InlineKeyboardMarkup]:
    ids = services.test_games.supervised_games()
    status = ", ".join(f"#{i}" for i in ids) if ids else "нет активных"
    text = "🎮 <b>ТЕСТОВАЯ ИГРА</b>\n\n" \
           f"Активные: <b>{esc(status)}</b>\n\n" \
           "▶️ Запустить — выбрать число ботов (4–8).\n" \
           "⚡️ Быстрый режим — 5 ботов, таймеры по 5 сек."
    kb = _rows(
        [_btn("4", "tg_run", "4"), _btn("5", "tg_run", "5"),
         _btn("6", "tg_run", "6"), _btn("7", "tg_run", "7"), _btn("8", "tg_run", "8")],
        [_btn("⚡️ Быстрый режим", "tg_fast"), _btn("📊 Статус", "tg_status")],
        [_btn("🛑 Остановить", "tg_stop")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return text, kb


def _screen_debug() -> tuple[str, InlineKeyboardMarkup]:
    text = "🧪 <b>DEBUG</b>\n\n" \
           "📊 Статус — флаги DEBUG_* и твой уровень.\n" \
           "🎮 Debug игры — список тест-игр.\n" \
           "📦 Состояние — дамп текущей игры.\n" \
           "⏭ Завершить фазу — пропуск текущей фазы."
    kb = _rows(
        [_btn("📊 Статус", "dbg_status"), _btn("🎮 Debug игры", "dbg_games")],
        [_btn("📦 Состояние", "dbg_state"), _btn("⏭ Завершить фазу", "dbg_finish")],
        [_btn("▶️ Test Game", "testgame")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return text, kb


def _screen_system() -> tuple[str, InlineKeyboardMarkup]:
    text = "⚙️ <b>СИСТЕМА</b>\n\n" \
           "📋 Инфо — статистика бота.\n" \
           "🔄 Reload — сброс кэша настроек.\n" \
           "🛠 Maintenance — режим обслуживания.\n" \
           "📜 Logs — хвост журнала.\n" \
           "📣 Broadcast — рассылка (ввод текста)."
    kb = _rows(
        [_btn("📋 Инфо о боте", "sys_info"), _btn("🔄 Reload", "sys_reload")],
        [_btn("🛠 Maintenance", "sys_maint"), _btn("📜 Logs", "sys_logs")],
        [_btn("📣 Broadcast", "sys_broadcast")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return text, kb


def _screen_staff(services, group=None) -> tuple[str, InlineKeyboardMarkup]:
    owners = services.settings.owner_id_list() if hasattr(services, "settings") else []
    admins = services.settings.admin_id_list() if hasattr(services, "settings") else []
    lines = ["🔨 <b>АДМИНИСТРАЦИЯ</b>", "",
             f"👑 OWNER_IDS: {', '.join(f'<code>{i}</code>' for i in owners) or '—'}",
             f"🎖 ADMIN_IDS: {', '.join(f'<code>{i}</code>' for i in admins) or '—'}"]
    if group is not None:
        lines += ["", f"🏠 Группа: <b>{esc(group.title)}</b> — /staff в группе"]
    kb = _rows(
        [_btn("🔎 Проверить права", "act", "check_rights"),
         _btn("👥 Игроки", "players")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return "\n".join(lines), kb


def _screen_players() -> tuple[str, InlineKeyboardMarkup]:
    text = ("👥 <b>ИГРОКИ</b>\n\n"
            "🔎 Найти игрока — пришли ID или @username.\n"
            "📋 Последние игроки — 10 новых регистраций.")
    kb = _rows(
        [_btn("🔎 Найти игрока", "act", "find"), _btn("📋 Последние", "players_recent")],
        [_btn("🏆 Рейтинги", "ratings"), _btn("🏅 Достижения", "achievements")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return text, kb


def _screen_xp() -> tuple[str, InlineKeyboardMarkup]:
    text = ("✨ <b>XP И УРОВНИ</b>\n\n"
            "Изменение — после выбора игрока. Формула та же, что и в играх\n"
            "(100 XP до 2-го уровня, дальше +50 к порогу).")
    kb = _rows(
        [_btn("➕ Добавить XP", "act", "xp_add"), _btn("✏️ Установить XP", "act", "xp_set")],
        [_btn("📈 Установить уровень", "act", "level_set"),
         _btn("📊 Таблица уровней", "leveltable")],
        [_btn("🏆 Рейтинги", "ratings"), _btn("👤 Игрок", "players")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    return text, kb


# ------------------------------------------------------------------ применение действий

_ACT_TITLES = {
    "rating_add": "общий рейтинг", "rating_set": "общий рейтинг",
    "wins_add": "победы", "wins_set": "победы",
    "xp_add": "XP", "xp_set": "XP", "level_set": "уровень",
}


def _confirm_text(player, action: str, value: str | None, current: str) -> str:
    title = _ACT_TITLES.get(action, action)
    name = esc(player.display_name or player.username or player.telegram_id)
    if action in ("ach_grant", "ach_revoke"):
        verb = "ВЫДАТЬ ДОСТИЖЕНИЕ?" if action == "ach_grant" else "СНЯТЬ ДОСТИЖЕНИЕ?"
        return f"⚠️ <b>{verb}</b>\n\nИгрок: {name}\nДостижение: <code>{esc(value)}</code>"
    if action in ("title_grant", "title_remove"):
        verb = "ВЫДАТЬ ТИТУЛ?" if action == "title_grant" else "СНЯТЬ ТИТУЛ?"
        return f"⚠️ <b>{verb}</b>\n\nИгрок: {name}\nТитул: <code>{esc(value)}</code>"
    if action == "reward_grant":
        return f"⚠️ <b>ВЫДАТЬ НАГРАДУ?</b>\n\nИгрок: {name}\nНаграда: <code>{esc(value)}</code>"
    if action == "reward_revoke":
        return f"⚠️ <b>ЗАБРАТЬ НАГРАДУ?</b>\n\nИгрок: {name}\nID награды: <code>{esc(value)}</code>"
    if action == "maint":
        return (f"⚠️ <b>РЕЖИМ ОБСЛУЖИВАНИЯ</b>\n\n"
                f"Сейчас: {'ВКЛ' if value == 'on' else 'ВЫКЛ'} → "
                f"станет: {'ВЫКЛ' if value == 'on' else 'ВКЛ'}")
    # числовые
    return (f"⚠️ <b>ИЗМЕНИТЬ {title.upper()}?</b>\n\n"
            f"Игрок: {name}\nБыло: <b>{current}</b>\nСтанет: <b>{esc(value)}</b>")


def _confirm_kb(encoded: str) -> InlineKeyboardMarkup:
    return _rows(
        [_btn("✅ Подтвердить", "confirm", encoded), _btn("❌ Отмена", "cancel")],
    )


async def _apply(session, services, actor_id: int, encoded: str) -> str:
    """Применяет подтверждённое действие (encoded — как в callback ✅)."""
    from bot.database.repositories.groups import AuditLogRepository
    from bot.database.repositories.social import (
        UserAchievementRepository,
        UserEventRewardRepository,
        UserTitleRepository,
    )
    from bot.database.repositories.users import UserRepository
    from bot.services import achievements as ach
    from bot.services import rewards as rw
    from bot.services.progression import DEFAULT_PROGRESSION as prog

    parts = encoded.split(".")
    kind = parts[0]
    repo = UserRepository(session)

    async def _audit(action: str, details: str, target_id: int | None = None) -> None:
        await AuditLogRepository(session).log(
            actor_id=actor_id, target_id=target_id, group_id=None,
            action=f"owner_{action}"[:48], details=details[:512],
        )

    if kind in ("rating_add", "rating_set", "wins_add", "wins_set", "xp_add", "xp_set",
                "level_set"):
        user = await repo.get_by_id(int(parts[1]))
        if user is None:
            return "❌ Игрок не найден."
        raw = parts[2]
        if kind == "level_set":
            if not raw.isdigit() or not 1 <= int(raw) <= 99:
                return "❌ Уровень должен быть числом 1–99."
            old = user.level
            user.level = int(raw)
            user.xp = prog.threshold(user.level)  # XP синхронизируется
            await _audit("set_level", f"{old} -> {user.level} xp={user.xp}", user.id)
            await session.commit()
            return f"✅ Уровень изменён: <b>{old} → {user.level}</b> (XP: {user.xp})"
        if not raw.lstrip("-").isdigit():
            return "❌ Нужно число."
        value = int(raw)
        if kind.startswith("rating"):
            old = user.rating
            user.rating = value if kind == "rating_set" else max(0, user.rating + value)
            await _audit(kind, f"{old} -> {user.rating}", user.id)
            result = f"✅ Общий рейтинг изменён: <b>{old} → {user.rating}</b>"
        elif kind.startswith("wins"):
            old = user.wins
            user.wins = value if kind == "wins_set" else max(0, user.wins + value)
            await _audit(kind, f"{old} -> {user.wins}", user.id)
            result = f"✅ Победы изменены: <b>{old} → {user.wins}</b>"
        else:  # xp
            old = user.xp
            user.xp = value if kind == "xp_set" else max(0, user.xp + value)
            user.level = prog.level_for_xp(user.xp)  # уровень пересчитывается сам
            await _audit(kind, f"{old} -> {user.xp} level={user.level}", user.id)
            result = f"✅ XP изменён: <b>{old} → {user.xp}</b> (уровень: {user.level})"
        await session.commit()
        return result

    if kind == "ach":
        user = await repo.get_by_id(int(parts[1]))
        aid, verb = parts[2], parts[3]
        definition = ach.get_achievement(aid)
        if user is None or definition is None:
            return "❌ Игрок или достижение не найдены."
        if verb == "grant":
            newly = await rw.award_achievements(session, {user.id: {aid}})
            await _audit("ach_grant", aid, user.id)
            await session.commit()
            if newly:
                return f"✅ {definition.emoji} «{definition.name}» выдан."
            return "Это достижение уже есть у игрока."
        removed = await UserAchievementRepository(session).remove(user.id, aid)
        if removed:
            from bot.services import titles as ttl

            title_id = ttl.TITLE_UNLOCKS.get(aid)
            if title_id:
                await UserTitleRepository(session).remove(user.id, title_id, source="achievement")
                if user.active_title == title_id:
                    user.active_title = None
            await _audit("ach_revoke", aid, user.id)
            await session.commit()
            return f"✅ «{definition.name}» снят."
        return "Такого достижения у игрока нет."

    if kind == "title":
        user = await repo.get_by_id(int(parts[1]))
        tid, verb = parts[2], parts[3]
        from bot.services import titles as ttl

        if user is None or ttl.get_title(tid) is None:
            return "❌ Игрок или титул не найдены."
        title_repo = UserTitleRepository(session)
        if verb == "grant":
            await title_repo.unlock(user.id, tid, source="admin")
            await _audit("title_grant", tid, user.id)
            await session.commit()
            return f"✅ Титул «{ttl.get_title(tid).name}» выдан."
        removed = await title_repo.remove(user.id, tid)
        if user.active_title == tid:
            user.active_title = None
        await _audit("title_remove", tid, user.id)
        await session.commit()
        return f"✅ Титул «{ttl.get_title(tid).name}» снят." if removed else "У игрока нет такого титула."

    if kind == "reward":
        user = await repo.get_by_id(int(parts[1]))
        if user is None:
            return "❌ Игрок не найден."
        if parts[2] == "grant":
            code = parts[3]
            ok, msg = await services.rewards.grant(user.id, code, actor_id)
            await _audit("reward_grant", code, user.id)
            await session.commit()
            return f"✅ {esc(msg)}" if ok else f"❌ {esc(msg)}"
        row_id = int(parts[2])
        rewards_repo = UserEventRewardRepository(session)
        row = await rewards_repo.get(row_id)
        if row is None or row.user_id != user.id:
            return "❌ Награда не найдена."
        reward = row.reward
        await session.delete(row)
        if user.active_event_reward_id == row_id:
            user.active_event_reward_id = None
        await _audit("reward_revoke", f"row={row_id} code={reward.code}", user.id)
        await session.commit()
        return f"✅ Награда «{esc(reward.name)}» забрана."

    if kind == "maint":
        from bot.database.repositories.settings import AppSettingRepository

        settings_repo = AppSettingRepository(session)
        stored = await settings_repo.get_global()
        enabled = parts[1] == "on"
        stored["maintenance"] = enabled
        await settings_repo.set_global(stored)
        await session.commit()
        if services.maintenance is not None:
            services.maintenance.invalidate()
        await _audit("maintenance", str(enabled))
        return f"🛠 Режим обслуживания: {'ВКЛ' if enabled else 'ВЫКЛ'}"

    return "❌ Неизвестное действие."


# ------------------------------------------------------------------ команда /owner

@router.message(Command("owner"))
async def cmd_owner(message: Message, services, db_user) -> None:
    """OWNER-панель: главное меню. Доступ — исключительно OWNER_IDS."""
    if not _is_owner(services, message.from_user.id):
        _deny_msg(message)
        return
    await message.answer(
        f"👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>\n\nПривет, {esc(db_user.display_name or 'Owner')}! "
        "Управление — кнопками ниже.",
        reply_markup=_main_kb(),
    )


# ------------------------------------------------------------------ навигация

async def _render(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    await callback.answer()
    await edit_or_answer(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "main"))
async def cb_main(callback: CallbackQuery, callback_data: OwnerCB, services) -> None:
    if not await _guard_cb(callback, services):
        return
    await _render(callback, "👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>", _main_kb())


@router.callback_query(OwnerCB.filter(F.action == "close"))
async def cb_close(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001 — сообщение могло стать недоступным
        await edit_or_answer(callback, "👑 Панель закрыта. Открыть снова: /owner")


@router.callback_query(OwnerCB.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, callback_data: OwnerCB, services, state: FSMContext) -> None:
    if not await _guard_cb(callback, services):
        return
    await state.clear()
    await _render(callback, "👑 <b>ПАНЕЛЬ ВЛАДЕЛЬЦА</b>", _main_kb())


@router.callback_query(OwnerCB.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    await _render(callback, await _screen_stats(session), _back_kb())


@router.callback_query(OwnerCB.filter(F.action == "players"))
async def cb_players(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_players()
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "players_recent"))
async def cb_players_recent(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    from bot.database.repositories.users import UserRepository

    users = await UserRepository(session).recent(10)
    lines = ["📋 <b>ПОСЛЕДНИЕ ИГРОКИ</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for u in users:
        lines.append(f"• {esc(u.display_name or u.username or u.telegram_id)} — "
                     f"<code>{u.telegram_id}</code> · ⭐{u.rating}")
        rows.append([_btn(f"👤 {u.display_name or u.username or u.telegram_id}",
                          "player", str(u.id))])
    rows.append([_btn("◀️ Назад", "players"), _btn("❌ Закрыть", "close")])
    await _render(callback, "\n".join(lines), _rows(*rows))


@router.callback_query(OwnerCB.filter(F.action == "player"))
async def cb_player(callback: CallbackQuery, callback_data: OwnerCB, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    what, _, uid = callback_data.value.partition(".")
    if not uid:  # просто '<user_id>' — карточка игрока
        what, uid = "", what
    if what == "ratings":
        text, kb = await _screen_player_ratings(session, int(uid))
    elif what == "xp":
        text, kb = await _screen_player_xp(session, int(uid))
    elif what == "ach":
        text, kb = await _screen_player_ach(session, int(uid))
    elif what == "titles":
        text, kb = await _screen_player_titles(session, int(uid))
    elif what == "rewards":
        text, kb = await _screen_player_rewards(session, int(uid))
    else:
        text, kb = await _screen_player(session, services, int(uid))
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "ratings"))
async def cb_ratings(callback: CallbackQuery, callback_data: OwnerCB, services, session, group) -> None:
    if not await _guard_cb(callback, services):
        return
    scope, _, metric = (
        callback_data.value.partition(".") if callback_data.value else ("global", "", "rating")
    )
    text, kb = await _screen_ratings(session, scope, metric or "rating", group)
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "xp"))
async def cb_xp(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_xp()
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "leveltable"))
async def cb_leveltable(callback: CallbackQuery, callback_data: OwnerCB, services) -> None:
    if not await _guard_cb(callback, services):
        return
    page = int(callback_data.value) if callback_data.value.isdigit() else 0
    text, kb = _screen_leveltable(page)
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопка-индикатор страницы (ничего не меняет)."""
    await callback.answer()


@router.callback_query(OwnerCB.filter(F.action == "achievements"))
async def cb_achievements(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = await _screen_achievements(session)
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "titles"))
async def cb_titles(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = await _screen_titles(session)
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "rewards"))
async def cb_rewards(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    catalog = await services.rewards.list_catalog()
    kb = _rows(
        [_btn("➕ Создать награду", "act", "reward_create"),
         _btn("🎁 Выдать", "act", "reward_grant")],
        [_btn("👤 Игроки", "players")],
        [_btn("◀️ Назад", "main"), _btn("❌ Закрыть", "close")],
    )
    await _render(callback, await _rewards_text(catalog), kb)


@router.callback_query(OwnerCB.filter(F.action == "testgame"))
async def cb_testgame(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_testgame(services)
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "debug"))
async def cb_debug(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_debug()
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "system"))
async def cb_system(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_system()
    await _render(callback, text, kb)


@router.callback_query(OwnerCB.filter(F.action == "staff"))
async def cb_staff(callback: CallbackQuery, services, session, group) -> None:
    if not await _guard_cb(callback, services):
        return
    text, kb = _screen_staff(services, group)
    await _render(callback, text, kb)


# ------------------------------------------------------------------ запуск действий

@router.callback_query(OwnerCB.filter(F.action == "act"))
async def cb_act(callback: CallbackQuery, callback_data: OwnerCB, services, session,
                state: FSMContext, group) -> None:
    """Старт действия. value = '<action>' или '<action>:<user_id>'."""
    if not await _guard_cb(callback, services):
        return
    action, _, uid = callback_data.value.partition(".")
    data: dict = {"action": action}

    if uid:
        # кнопки шифруют user.id (DB), а не telegram_id
        from bot.database.repositories.users import UserRepository

        target = await UserRepository(session).get_by_id(int(uid))
        if target is None:
            await callback.answer("Игрок не найден", show_alert=True)
            return
        data["target_id"] = target.id
        if action in _ACT_TITLES:  # числовые метрики
            await _start_value_step(callback, state, data, session, target)
            return
        if action == "reward_grant":
            await _render(callback, _pick_reward_text(target), await _pick_reward(services, target))
            return
        if action in ("ach_grant", "ach_revoke"):
            text, kb = await _screen_player_ach(session, target.id)
            await _render(callback, text, kb)
            return
        if action in ("title_grant", "title_remove"):
            text, kb = await _screen_player_titles(session, target.id)
            await _render(callback, text, kb)
            return

    if action == "find":
        await state.set_state(OwnerStates.player_input)
        await state.update_data(action="find")
        await _render(callback, "🔎 Пришли <b>ID</b> или <b>@username</b> игрока:",
                      _cancel_kb())
    elif action == "check_rights":
        await state.set_state(OwnerStates.player_input)
        await state.update_data(action="check_rights")
        await _render(callback, "🔎 Пришли <b>ID</b> или <b>@username</b> игрока "
                      "для проверки прав:", _cancel_kb())
    elif action == "reward_create":
        await state.set_state(OwnerStates.reward_input)
        await state.update_data(action="reward_create")
        await _render(
            callback,
            "➕ Создание награды. Пришли строку:\n"
            "<code>code|emoji|name|kind|дни|описание</code>\n"
            "kind: event|tournament|role|special. Дни — число или пусто (бессрочно).",
            _cancel_kb("rewards"),
        )
    else:  # rating/wins/xp/level/ach/title/reward — сначала игрок
        await state.set_state(OwnerStates.player_input)
        await state.update_data(action=action)
        await _render(callback, "👤 Пришли <b>ID</b> или <b>@username</b> игрока:",
                      _cancel_kb())


async def _resolve_user(session, query: str):
    from bot.services.lookup import UserLookupService

    return await UserLookupService(session).resolve(query=query.strip() or None,
                                                    reply_telegram_id=None)


def _current_value(user, action: str) -> str:
    return {
        "rating_add": str(user.rating), "rating_set": str(user.rating),
        "wins_add": str(user.wins), "wins_set": str(user.wins),
        "xp_add": str(user.xp), "xp_set": str(user.xp), "level_set": str(user.level),
    }.get(action, "?")


async def _start_value_step(callback: CallbackQuery, state: FSMContext, data: dict,
                            session, target) -> None:
    """Действие с числом: игрок выбран — просим значение."""
    action = data["action"]
    await state.set_state(OwnerStates.value_input)
    await state.update_data(**data)
    prompt = {
        "rating_add": "➕ Добавить к общему рейтингу",
        "rating_set": "✏️ Новый общий рейтинг",
        "wins_add": "➕ Добавить побед",
        "wins_set": "✏️ Новое число побед",
        "xp_add": "➕ Добавить XP",
        "xp_set": "✏️ Новый XP",
        "level_set": "📈 Новый уровень (1–99); XP подстроится",
    }.get(action, "Значение")
    name = esc(target.display_name or target.username or target.telegram_id)
    await _render(
        callback,
        f"👤 <b>{name}</b>\n\nТекущее значение:\n{_current_value(target, action)}\n\n"
        f"{prompt} (числом):",
        _cancel_kb(),
    )


# ---- выбор предмета (достижение/титул/награда) после выбора игрока

def _pick_reward_text(target) -> str:
    return (f"🎁 Кому: <b>{esc(target.display_name or target.telegram_id)}</b>. "
            "Выбери награду:")


async def _pick_reward(services, target) -> InlineKeyboardMarkup:
    """Каталог наград кнопками (для выдачи выбранному игроку)."""
    catalog = await services.rewards.list_catalog()
    rows = [[_btn(f"{r.emoji} {r.name}", "item", f"rewardgrant.{target.id}.{r.code}")]
            for r in catalog]
    rows.append([_btn("◀️ Назад", "player", str(target.id)), _btn("❌ Закрыть", "close")])
    return _rows(*rows)


@router.callback_query(OwnerCB.filter(F.action == "item"))
async def cb_item(callback: CallbackQuery, callback_data: OwnerCB, services, session) -> None:
    """value = '<kind>:<user_id>:<item>' — показать подтверждение действия."""
    if not await _guard_cb(callback, services):
        return
    parts = callback_data.value.split(".")
    kind, uid, item = parts[0], parts[1], parts[2]
    from bot.database.repositories.users import UserRepository

    user = await UserRepository(session).get_by_id(int(uid))
    if user is None:
        await callback.answer("Игрок не найден", show_alert=True)
        return

    if kind == "ach":  # наличие определяет глагол
        from bot.database.repositories.social import UserAchievementRepository

        has = await UserAchievementRepository(session).has(int(uid), item)
        action, verb = ("ach_revoke", "revoke") if has else ("ach_grant", "grant")
        encoded = f"ach.{uid}.{item}.{verb}"
    elif kind == "title":
        from bot.database.repositories.social import UserTitleRepository

        has = item in (await UserTitleRepository(session).ids_of(int(uid)))
        action, verb = ("title_remove", "remove") if has else ("title_grant", "grant")
        encoded = f"title.{uid}.{item}.{verb}"
    else:  # rewardgrant:<uid>:<code> | reward:<uid>:<row_id>
        action = "reward_grant" if kind == "rewardgrant" else "reward_revoke"
        encoded = (f"reward.{uid}.grant.{item}" if action == "reward_grant"
                   else f"reward.{uid}.{item}")
    await callback.answer()
    await edit_or_answer(
        callback,
        _confirm_text(user, action, item, _current_value(user, action)),
        _confirm_kb(encoded),
    )


# ------------------------------------------------------------------ подтверждение

@router.callback_query(OwnerCB.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, callback_data: OwnerCB, services,
                     session, state: FSMContext, db_user) -> None:
    if not await _guard_cb(callback, services):
        return
    await state.clear()
    result = await _apply(session, services, db_user.id, callback_data.value)
    kb = _rows([_btn("◀️ В меню", "main"), _btn("❌ Закрыть", "close")])
    await _render(callback, result, kb)


# ------------------------------------------------------------------ FSM: ввод игрока

@router.message(OwnerStates.player_input)
async def process_player_input(message: Message, state: FSMContext, session, services) -> None:
    if not _is_owner(services, message.from_user.id):
        _deny_msg(message)
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("👑 Панель: /owner", reply_markup=_main_kb())
        return
    data = await state.get_data()
    action = data.get("action", "find")
    target = await _resolve_user(session, message.text or "")
    if target is None:
        await message.answer("🤷 Не найден. Пришли ID или @username ещё раз, /cancel — отмена.")
        return
    await state.clear()

    if action == "find":
        text, kb = await _screen_player(session, services, target.id)
        await message.answer(text, reply_markup=kb)
        return
    if action == "check_rights":
        from bot.database.repositories.groups import GroupAdminRepository, GroupRepository

        level = services.permissions.global_level(target.telegram_id)
        lines = [f"🔐 <b>ПРАВА</b> · {esc(target.display_name or target.telegram_id)}",
                 f"ID: <code>{target.telegram_id}</code>", "",
                 f"Глобальный уровень: <b>{level.name}</b> ({level.value})"]
        groups = await GroupRepository(session).all()
        ga = GroupAdminRepository(session)
        for g in groups:
            lvl = await ga.level_of(g.id, target.id)
            if lvl:
                lines.append(f"🏠 {esc(g.title)} — уровень {lvl}")
        await message.answer("\n".join(lines), reply_markup=_back_kb("staff"))
        return
    if action in ("ach_grant", "ach_revoke"):
        # показываем список достижений игрока с кнопками ➕/❌
        text, kb = await _screen_player_ach(session, target.id)
        await message.answer(text, reply_markup=kb)
        return
    if action in ("title_grant", "title_remove"):
        text, kb = await _screen_player_titles(session, target.id)
        await message.answer(text, reply_markup=kb)
        return
    if action == "reward_grant":
        await message.answer(_pick_reward_text(target),
                             reply_markup=await _pick_reward(services, target))
        return
    # числовые действия
    await _start_value_step_msg(message, state, {"action": action, "target_id": target.id},
                                session, target)


async def _start_value_step_msg(message: Message, state: FSMContext, data: dict,
                                session, target) -> None:
    action = data["action"]
    await state.set_state(OwnerStates.value_input)
    await state.update_data(**data)
    prompt = {
        "rating_add": "➕ Добавить к общему рейтингу",
        "rating_set": "✏️ Новый общий рейтинг",
        "wins_add": "➕ Добавить побед",
        "wins_set": "✏️ Новое число побед",
        "xp_add": "➕ Добавить XP",
        "xp_set": "✏️ Новый XP",
        "level_set": "📈 Новый уровень (1–99); XP подстроится",
    }.get(action, "Значение")
    name = esc(target.display_name or target.username or target.telegram_id)
    await message.answer(
        f"👤 <b>{name}</b>\n\nТекущее значение:\n{_current_value(target, action)}\n\n"
        f"{prompt} (числом):",
        reply_markup=_cancel_kb(),
    )


# ------------------------------------------------------------------ FSM: ввод числа

@router.message(OwnerStates.value_input)
async def process_value_input(message: Message, state: FSMContext, session, services) -> None:
    if not _is_owner(services, message.from_user.id):
        _deny_msg(message)
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("👑 Панель: /owner", reply_markup=_main_kb())
        return
    data = await state.get_data()
    action = data.get("action")
    target_id = data.get("target_id")
    if not action or not target_id:
        await state.clear()
        await message.answer("Сессия устарела. Начни заново: /owner")
        return
    raw = (message.text or "").strip().replace("−", "-")
    if not raw.lstrip("-").isdigit():
        await message.answer("Нужно целое число (можно с минусом). Попробуй ещё раз или /cancel.")
        return
    from bot.database.repositories.users import UserRepository

    target = await UserRepository(session).get_by_id(int(target_id))
    if target is None:
        await state.clear()
        await message.answer("Игрок не найден.")
        return
    encoded = f"{action}.{target_id}.{raw}"
    await state.clear()
    await message.answer(
        _confirm_text(target, action, raw, _current_value(target, action)),
        reply_markup=_confirm_kb(encoded),
    )


# ------------------------------------------------------------------ FSM: создание награды

@router.message(OwnerStates.reward_input)
async def process_reward_input(message: Message, state: FSMContext, session, services,
                               db_user) -> None:
    if not _is_owner(services, message.from_user.id):
        _deny_msg(message)
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        await message.answer("👑 Панель: /owner", reply_markup=_main_kb())
        return
    parts = (message.text or "").split("|")
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>code|emoji|name|kind|дни|описание</code>. Попробуй ещё раз или /cancel.")
        return
    code = parts[0].strip()
    emoji = parts[1].strip() or "🎁"
    name = parts[2].strip()
    kind = parts[3].strip() if len(parts) > 3 else "event"
    expires = int(parts[4].strip()) if len(parts) > 4 and parts[4].strip().isdigit() else None
    description = parts[5].strip() if len(parts) > 5 else ""
    await state.clear()
    ok, msg = await services.rewards.create_reward(
        code, name, emoji, description, kind, expires, db_user.id
    )
    await session.commit()
    kb = _rows([_btn("◀️ К наградам", "rewards"), _btn("❌ Закрыть", "close")])
    await message.answer(f"✅ {esc(msg)}" if ok else f"❌ {esc(msg)}", reply_markup=kb)


# ------------------------------------------------------------------ тестовая игра / debug / система

@router.callback_query(OwnerCB.filter(F.action == "tg_run"))
async def cb_tg_run(callback: CallbackQuery, callback_data: OwnerCB, services, session,
                    db_user, group) -> None:
    if not await _guard_cb(callback, services):
        return
    count = int(callback_data.value or 5)
    game_id, text = await services.test_games.create_test_game(
        db_user.id, count, fast=False, group_id=group.id if group else None,
    )
    if game_id is None:
        await callback.answer(text[:180], show_alert=True)
        return
    await services.audit.log(db_user.id, "testgame", None,
                             group.id if group else None, f"game={game_id} bots={count}")
    from bot.keyboards.testgame import test_controls_kb

    await _render(
        callback,
        f"{esc(text)}\n\n🎭 Твоя роль придёт отдельным сообщением.",
        test_controls_kb(game_id, auto_on=True),
    )


@router.callback_query(OwnerCB.filter(F.action == "tg_fast"))
async def cb_tg_fast(callback: CallbackQuery, services, session, db_user, group) -> None:
    if not await _guard_cb(callback, services):
        return
    game_id, text = await services.test_games.create_test_game(
        db_user.id, 5, fast=True, group_id=group.id if group else None,
    )
    if game_id is None:
        await callback.answer(text[:180], show_alert=True)
        return
    await services.audit.log(db_user.id, "testgame", None,
                             group.id if group else None, f"game={game_id} fast")
    from bot.keyboards.testgame import test_controls_kb

    await _render(
        callback,
        f"{esc(text)} ⚡️ fast\n\n🎭 Твоя роль придёт отдельным сообщением.",
        test_controls_kb(game_id, auto_on=True),
    )


@router.callback_query(OwnerCB.filter(F.action.in_({"tg_status", "dbg_state"})))
async def cb_tg_status(callback: CallbackQuery, callback_data: OwnerCB, services) -> None:
    if not await _guard_cb(callback, services):
        return
    ids = services.test_games.supervised_games()
    if not ids:
        await callback.answer("Активных тест-игр нет", show_alert=True)
        return
    await _render(callback, await services.test_games.dump_state(ids[0]),
                  _back_kb("testgame"))


@router.callback_query(OwnerCB.filter(F.action == "tg_stop"))
async def cb_tg_stop(callback: CallbackQuery, services, db_user) -> None:
    if not await _guard_cb(callback, services):
        return
    ids = services.test_games.supervised_games()
    if not ids:
        await callback.answer("Активных тест-игр нет", show_alert=True)
        return
    result = await services.test_games.finish(ids[0])
    await services.audit.log(db_user.id, "testgame_stop", None, None, f"game={ids[0]}")
    await _render(callback, esc(result), _back_kb("testgame"))


@router.callback_query(OwnerCB.filter(F.action == "dbg_status"))
async def cb_dbg_status(callback: CallbackQuery, services, session, group) -> None:
    if not await _guard_cb(callback, services):
        return
    from bot.config import get_settings

    settings = get_settings()
    level = services.permissions.global_level(callback.from_user.id)
    group_line = "—"
    if group is not None:
        group_settings = await services.groups.get_settings(group.id)
        group_line = f"{group.title}: debug_enabled={group_settings.debug_enabled}"
    await _render(
        callback,
        "🧪 <b>DEBUG MODE</b>\n\n"
        f"<code>DEBUG_MODE={settings.debug_mode}\n"
        f"DEBUG_AFFECTS_GLOBAL_STATS={settings.debug_affects_global_stats}\n"
        f"DEBUG_AFFECTS_LOCAL_STATS={settings.debug_affects_local_stats}</code>\n\n"
        f"Группа: {esc(group_line)}\nТвой уровень: <b>{level.name}</b>",
        _back_kb("debug"),
    )


@router.callback_query(OwnerCB.filter(F.action == "dbg_games"))
async def cb_dbg_games(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    ids = services.test_games.supervised_games()
    text = ("🧪 Активные тест-игры: "
            + (", ".join(f"#{i}" for i in ids) if ids else "нет"))
    await _render(callback, text, _back_kb("debug"))


@router.callback_query(OwnerCB.filter(F.action.in_({"dbg_finish", "dbg_phase"})))
async def cb_dbg_finish(callback: CallbackQuery, services, db_user) -> None:
    if not await _guard_cb(callback, services):
        return
    ids = services.test_games.supervised_games()
    if not ids:
        await callback.answer("Активных тест-игр нет", show_alert=True)
        return
    result = await services.test_games.skip_phase(ids[0])
    await services.audit.log(db_user.id, "debug_skip_phase", None, None, f"game={ids[0]}")
    await _render(callback, esc(result), _back_kb("debug"))


@router.callback_query(OwnerCB.filter(F.action == "sys_info"))
async def cb_sys_info(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    await _render(callback, await _screen_stats(session), _back_kb("system"))


@router.callback_query(OwnerCB.filter(F.action == "sys_reload"))
async def cb_sys_reload(callback: CallbackQuery, services, db_user) -> None:
    if not await _guard_cb(callback, services):
        return
    if services.maintenance is not None:
        services.maintenance.invalidate()
    await services.audit.log(db_user.id, "owner_reload", None, None, "")
    await _render(callback, "♻️ Кэш настроек сброшен.", _back_kb("system"))


@router.callback_query(OwnerCB.filter(F.action == "sys_maint"))
async def cb_sys_maint(callback: CallbackQuery, services, session) -> None:
    if not await _guard_cb(callback, services):
        return
    from bot.database.repositories.settings import AppSettingRepository

    stored = await AppSettingRepository(session).get_global()
    enabled = bool(stored.get("maintenance", False))
    await _render(
        callback,
        _confirm_text(None, "maint", "on" if enabled else "off", ""),
        _confirm_kb(f"maint.{'off' if enabled else 'on'}"),
    )


@router.callback_query(OwnerCB.filter(F.action == "sys_logs"))
async def cb_sys_logs(callback: CallbackQuery, services) -> None:
    if not await _guard_cb(callback, services):
        return
    from bot.config import get_settings
    from bot.utils.logging import read_log_tail

    tail = read_log_tail(get_settings().log_file, 40)
    await _render(callback, f"<code>{esc(tail[-3000:])}</code>", _back_kb("system"))


@router.callback_query(OwnerCB.filter(F.action == "sys_broadcast"))
async def cb_sys_broadcast(callback: CallbackQuery, services, state: FSMContext) -> None:
    """Переиспользует существующую FSM рассылки (/broadcast)."""
    if not await _guard_cb(callback, services):
        return
    from bot.states import AdminStates

    await state.set_state(AdminStates.broadcast_input)
    await _render(callback, "📣 Пришли текст рассылки (HTML разрешён).\n/cancel — отмена.",
                  _back_kb("system"))
