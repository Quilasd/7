"""История игр с пагинацией и детальным просмотром партии.

Данные берутся напрямую из Game/GamePlayer/GameAction/Vote (дубликатов нет):
состав, роли, длительность, победитель и таймлайн (game.events).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.models import PlayerStatus, WinningSide
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.roles import get_role
from bot.utils.callbacks import HistoryCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8
WINNER_LABEL = {
    WinningSide.CITY.value: "🔵 Город",
    WinningSide.MAFIA.value: "🟥 Мафия",
    WinningSide.MANIAC.value: "🔪 Маньяк",
    WinningSide.DRAW.value: "🤝 Ничья",
}


def _result_icon(gp, game) -> str:
    role = get_role(gp.role)
    winner = game.winner
    if winner == WinningSide.DRAW.value:
        return "🤝"
    won = (
        (winner == WinningSide.MAFIA.value and role and role.team.value == "mafia")
        or (winner == WinningSide.CITY.value and role and role.team.value == "city")
        or (winner == WinningSide.MANIAC.value and role and role.team.value == "neutral")
    )
    return "✅" if won else "❌"


def _duration(started_at, ended_at) -> str:
    if not started_at or not ended_at:
        return "—"
    delta = ended_at - started_at
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins} мин"
    return f"{mins // 60} ч {mins % 60} мин"


def _history_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=HistoryCB(action="page", page=page - 1).pack()))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(1, total_pages)}", callback_data=HistoryCB(action="page", page=page).pack()))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=HistoryCB(action="page", page=page + 1).pack()))
    return InlineKeyboardMarkup(inline_keyboard=[nav])


async def _render_list(session, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    repo = GamePlayerRepository(session)
    total = await repo.history_count(user_id)
    if total == 0:
        return "📜 Сыграемых игр пока нет.", None
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await repo.history_for_user(user_id, PAGE_SIZE, page * PAGE_SIZE)
    lines = [f"📜 <b>ИСТОРИЯ ИГР</b> (всего {total})", ""]
    for idx, gp in enumerate(rows, start=page * PAGE_SIZE + 1):
        game = gp.game
        role = get_role(gp.role)
        lines.append(
            f"{_result_icon(gp, game)} <b>{idx}.</b> Игра #{game.id} "
            f"— {role.title if role else '—'} · {WINNER_LABEL.get(game.winner, '—')}"
        )
        lines.append(
            f"     {esc(role.team.value if role else '—')} · {_duration(game.started_at, game.ended_at)}"
            f"\n     <i>Подробнее:</i> /game_{game.id}"
        )
    return "\n".join(lines), _history_kb(page, total_pages)


def _detail_text(game, players) -> str:
    role = None
    lines = [f"🎮 <b>Игра #{game.id}</b>", ""]
    lines.append(f"🏆 Исход: <b>{WINNER_LABEL.get(game.winner, '—')}</b>")
    if game.end_reason:
        lines.append(f"📝 {esc(game.end_reason)}")
    lines.append(f"⏱ Длительность: {_duration(game.started_at, game.ended_at)}")
    lines.append(f"🌙 Дней сыграно: <b>{game.day_number}</b>")
    lines.append("")
    lines.append("👥 <b>Состав:</b>")
    for p in sorted(players, key=lambda x: x.slot):
        r = get_role(p.role)
        state = "💀" if p.status in (PlayerStatus.DEAD.value, PlayerStatus.LEFT.value) else "🙂"
        lines.append(f"{state} {esc(display_name(p.user))} — {r.title if r else '—'}")
    events = game.events or []
    if events:
        lines.append("")
        lines.append("📋 <b>Хронология:</b>")
        shown = 0
        for e in events[-20:]:
            line = _event_line(e)
            if line:
                lines.append(line)
                shown += 1
        if len(events) > 20:
            lines.append(f"<i>…и ещё {len(events) - 20} событий</i>")
    return "\n".join(lines)


def _event_line(e: dict) -> str:
    t = e.get("type")
    day = e.get("day", "?")
    if t == "death":
        cause = {
            "mafia": "убит мафией", "maniac": "убит маньяком", "vote": "изгнан городом",
            "left": "покинул игру", "sacrifice": "погиб, спасая другого",
        }.get(e.get("cause"), "выбыл")
        return f"  • День {day}: игрок {cause}."
    if t == "save":
        return f"  • Ночь {day}: врач спас игрока."
    if t == "phase":
        return f"  • День {day}: {esc(str(e.get('phase', '')))}"
    return ""


async def _show_detail(message_or_cb, session, game_id: int) -> None:
    game = await GameRepository(session).get(game_id)
    is_msg = isinstance(message_or_cb, Message)
    if game is None:
        if is_msg:
            await message_or_cb.answer("Игра не найдена.")
        else:
            await message_or_cb.answer("Игра не найдена.", show_alert=True)
        return
    players = await GamePlayerRepository(session).list_for_game(game.id)
    text = _detail_text(game, players)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ К истории", callback_data=HistoryCB(action="page", page=0).pack())
    ]])
    if is_msg:
        await message_or_cb.answer(text, reply_markup=kb)
    else:
        await edit_or_answer(message_or_cb, text, kb)


@router.message(Command("history"))
async def cmd_history(message: Message, session, db_user) -> None:
    text, kb = await _render_list(session, db_user.id, 0)
    await message.answer(text, reply_markup=kb)


@router.message(F.text.regexp(r"^/game_(\d+)$"))
async def cmd_game_detail(message: Message, session) -> None:
    game_id = int(message.text.split("_")[1])
    await _show_detail(message, session, game_id)


@router.callback_query(HistoryCB.filter(F.action == "page"))
async def cb_history_page(callback: CallbackQuery, callback_data: HistoryCB, session, db_user) -> None:
    await callback.answer()
    text, kb = await _render_list(session, db_user.id, callback_data.page)
    await edit_or_answer(callback, text, kb)


@router.callback_query(HistoryCB.filter(F.action == "detail"))
async def cb_history_detail(callback: CallbackQuery, callback_data: HistoryCB, session) -> None:
    await callback.answer()
    await _show_detail(callback, session, callback_data.game_id)
