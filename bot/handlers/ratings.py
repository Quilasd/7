"""Рейтинги: глобальные и локальные (по группам), с пагинацией.

Команды: /top /top_rating /top_wins /top_levels /group_stats /global_stats
В группе по умолчанию показывается локальный рейтинг этой группы.
"""

from __future__ import annotations

import logging
import math

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.models import Group, GroupPlayer
from bot.database.repositories.groups import GroupPlayerRepository
from bot.database.repositories.users import UserRepository
from bot.utils.callbacks import MenuCB, RatingCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10

METRIC_TITLES = {
    "rating": "⭐ Рейтинг",
    "wins": "🏆 Победы",
    "level": "🎖 Уровни",
}


def _nav_kb(scope: str, metric: str, page: int, total_pages: int, in_group: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=RatingCB(scope=scope, metric=metric, page=page - 1).pack()))
    nav.append(InlineKeyboardButton(
        text=f"{page + 1}/{max(1, total_pages)}",
        callback_data=RatingCB(scope=scope, metric=metric, page=page).pack()))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=RatingCB(scope=scope, metric=metric, page=page + 1).pack()))
    if nav:
        rows.append(nav)

    switch: list[InlineKeyboardButton] = []
    if in_group:
        if scope != "local":
            switch.append(InlineKeyboardButton(
                text="🏠 Эта группа", callback_data=RatingCB(scope="local", metric=metric, page=0).pack()))
        if scope != "global":
            switch.append(InlineKeyboardButton(
                text="🌐 Глобальный", callback_data=RatingCB(scope="global", metric=metric, page=0).pack()))
    else:
        switch.append(InlineKeyboardButton(
            text="🌐 Глобальный", callback_data=RatingCB(scope="global", metric=metric, page=0).pack()))
    rows.append(switch)
    rows.append([
        InlineKeyboardButton(text="⭐ Рейтинг", callback_data=RatingCB(scope=scope, metric="rating", page=0).pack()),
        InlineKeyboardButton(text="🏆 Победы", callback_data=RatingCB(scope=scope, metric="wins", page=0).pack()),
        InlineKeyboardButton(text="🎖 Уровни", callback_data=RatingCB(scope=scope, metric="level", page=0).pack()),
    ])
    rows.append([InlineKeyboardButton(
        text="⬅️ Меню", callback_data=MenuCB(action="main").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _global_page(session, metric: str, page: int) -> tuple[str, int]:
    repo = UserRepository(session)
    users = await repo.top_by_rating(1000)  # выборка, сортировка ниже по метрике
    if metric == "wins":
        users = sorted(users, key=lambda u: (-u.wins, -u.games_played))
    elif metric == "level":
        users = sorted(users, key=lambda u: (-u.level, -u.xp))
    total_pages = max(1, math.ceil(len(users) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"🌐 <b>ГЛОБАЛЬНЫЙ РЕЙТИНГ · {METRIC_TITLES[metric]}</b>", ""]
    if not chunk:
        lines.append("Пока пусто.")
    for index, user in enumerate(chunk, start=page * PAGE_SIZE + 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        value = {"rating": user.rating, "wins": user.wins, "level": user.level}[metric]
        lines.append(f"{medal} {esc(display_name(user))} — {value}")
    return "\n".join(lines), total_pages


def _local_page_text(players: list[GroupPlayer], metric: str, page: int, group: Group) -> tuple[str, int]:
    total_pages = max(1, math.ceil(len(players) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    chunk = players[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    lines = [f"🏠 <b>{esc(group.title or 'ЭТА ГРУППА')}</b>", f"Локальный топ · {METRIC_TITLES[metric]}", ""]
    if not chunk:
        lines.append("В этой группе пока никто не играл.")
    for index, gp in enumerate(chunk, start=page * PAGE_SIZE + 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        value = {"rating": gp.rating, "wins": gp.wins, "level": gp.level}[metric]
        lines.append(f"{medal} {esc(display_name(gp.user))} — {value}")
    return "\n".join(lines), total_pages


async def show_rating(event_like, session, group: Group | None, scope: str, metric: str, page: int):
    """Общая отрисовка (сообщение или колбэк)."""
    in_group = group is not None
    if scope == "local" and group is None:
        scope = "global"

    if scope == "local":
        players = await GroupPlayerRepository(session).top(group.id, metric, limit=1000)
        text, total_pages = _local_page_text(players, metric, page, group)
    else:
        text, total_pages = await _global_page(session, metric, page)

    keyboard = _nav_kb(scope, metric, max(0, min(page, total_pages - 1)), total_pages, in_group)
    if isinstance(event_like, CallbackQuery):
        await event_like.answer()
        await edit_or_answer(event_like, text, keyboard)
    else:
        await event_like.answer(text, reply_markup=keyboard)


def _scope_and_metric(command: str, group: Group | None, requested: str | None):
    metric = {"top_wins": "wins", "top_levels": "level"}.get(command, "rating")
    scope = "local" if group is not None else "global"
    if requested in ("global", "local"):
        scope = requested
    if requested in METRIC_TITLES:
        metric = requested
    return scope, metric


@router.message(Command("top", "top_rating", "top_wins", "top_levels"))
async def cmd_top(message: Message, command: CommandObject, session, group):
    requested = (command.args or "").strip().lower() or None
    scope, metric = _scope_and_metric(command.command, group, requested)
    await show_rating(message, session, group, scope, metric, 0)


@router.callback_query(RatingCB.filter())
async def cb_rating(callback: CallbackQuery, callback_data: RatingCB, session, group) -> None:
    await show_rating(
        callback, session, group, callback_data.scope, callback_data.metric, callback_data.page
    )


@router.callback_query(MenuCB.filter(F.action == "rating"))
async def cb_menu_rating(callback: CallbackQuery, session, group) -> None:
    # В группе сразу локальный топ, в личке — глобальный + меню переходов
    scope = "local" if group is not None else "global"
    await show_rating(callback, session, group, scope, "rating", 0)


@router.message(Command("group_stats"))
async def cmd_group_stats(message: Message, session, group) -> None:
    if group is None:
        await message.answer("Команда работает только в группе.")
        return
    repo = GroupPlayerRepository(session)
    players = await repo.list_for_group(group.id, limit=1000)
    total_games = sum(gp.games_played for gp in players)
    total_wins = sum(gp.wins for gp in players)
    total_xp = sum(gp.xp for gp in players)
    top = await repo.top(group.id, "rating", limit=3)
    lines = [
        f"🏠 <b>СТАТИСТИКА ГРУППЫ</b>\n<i>{esc(group.title or '')}</i>",
        "",
        f"👥 Игроков: <b>{len(players)}</b>",
        f"🎮 Сыграно игр (суммарно): <b>{total_games}</b>",
        f"🏆 Суммарные победы: <b>{total_wins}</b>",
        f"✨ Суммарный XP: <b>{total_xp}</b>",
    ]
    if top:
        lines += ["", "Топ-3 по рейтингу:"]
        for index, gp in enumerate(top, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
            lines.append(f"{medal} {esc(display_name(gp.user))} — {gp.rating}")
    await message.answer("\n".join(lines))


@router.message(Command("global_stats"))
async def cmd_global_stats(message: Message, session) -> None:
    users_repo = UserRepository(session)
    from bot.database.repositories.games import GameRepository

    games_repo = GameRepository(session)
    total_users = await users_repo.count_all()
    active = await games_repo.count_active()
    finished = await games_repo.count_finished()
    top = await users_repo.top_by_rating(3)
    lines = [
        "🌐 <b>ГЛОБАЛЬНАЯ СТАТИСТИКА</b>",
        "",
        f"👤 Пользователей: <b>{total_users}</b>",
        f"🎮 Активных игр: <b>{active}</b>",
        f"🏁 Завершённых игр: <b>{finished}</b>",
    ]
    if top:
        lines += ["", "Топ-3 по рейтингу:"]
        for index, user in enumerate(top, start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
            lines.append(f"{medal} {esc(display_name(user))} — {user.rating} (Lvl {user.level})")
    await message.answer("\n".join(lines))
