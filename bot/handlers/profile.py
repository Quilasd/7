"""Профиль: статистика, смена имени, история игр, настройки."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.repositories.games import GamePlayerRepository
from bot.keyboards.common import back_to_menu_kb
from bot.states import ProfileStates
from bot.utils.callbacks import MenuCB, ProfileCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()


def profile_text(user, header: bool = True, progression=None) -> str:
    """Глобальный блок профиля (статистика User, не зависит от групп)."""
    from bot.services.progression import DEFAULT_PROGRESSION

    progression = progression or DEFAULT_PROGRESSION
    total = user.wins + user.losses
    winrate = (user.wins / total * 100) if total else 0.0
    _, in_level, need = progression.xp_progress_in_level(user.xp)
    lines = [
        "🌐 <b>ГЛОБАЛЬНО</b>",
        "",
        f"⭐ Рейтинг: <b>{user.rating}</b>",
        f"🎖 Уровень: <b>{user.level}</b>",
        f"✨ XP: <b>{user.xp}</b> (до следующего уровня {in_level}/{need})",
        f"🏆 Побед: <b>{user.wins}</b>",
        f"💀 Поражений: <b>{user.losses}</b>",
        f"🎮 Игр: <b>{user.games_played}</b>",
        f"📈 Winrate: <b>{winrate:.0f}%</b>",
        "",
        f"☠️ Убийств: <b>{user.kills}</b>",
        f"❤️ Спасений: <b>{user.saves}</b>",
        f"🕵️ Расследований: <b>{user.investigations}</b>",
        f"🗳 Правильных голосований: <b>{user.correct_votes}</b>",
    ]
    if header:
        head = [
            f"👤 <b>{esc(display_name(user))}</b>",
            f"ID: <code>{user.telegram_id}</code>",
            "",
        ]
        lines = head + lines
    return "\n".join(lines)


def profile_group_block(group, group_player, progression=None) -> str:
    """Локальный блок: статистика игрока В КОНКРЕТНОЙ группе (GroupPlayer)."""
    from bot.services.progression import DEFAULT_PROGRESSION

    progression = progression or DEFAULT_PROGRESSION
    gp = group_player
    total = gp.wins + gp.losses
    winrate = (gp.wins / total * 100) if total else 0.0
    _, in_level, need = progression.xp_progress_in_level(gp.xp)
    return "\n".join([
        f"🏠 <b>ЭТА ГРУППА</b>\n<i>{esc(group.title or '')}</i>\n",
        f"⭐ Рейтинг: <b>{gp.rating}</b>",
        f"🎖 Уровень: <b>{gp.level}</b>",
        f"✨ XP: <b>{gp.xp}</b> (до следующего уровня {in_level}/{need})",
        f"🏆 Побед: <b>{gp.wins}</b>",
        f"💀 Поражений: <b>{gp.losses}</b>",
        f"🎮 Игр: <b>{gp.games_played}</b>",
        f"📈 Winrate: <b>{winrate:.0f}%</b>",
        "",
        f"☠️ Убийств: <b>{gp.kills}</b> · ❤️ Спасений: <b>{gp.saves}</b>",
        f"🕵️ Расследований: <b>{gp.investigations}</b> · 🗳 Верный голос: <b>{gp.correct_votes}</b>",
    ])


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✏️ Изменить имя", callback_data=ProfileCB(action="name").pack()
        ),
        InlineKeyboardButton(
            text="📜 Мои игры", callback_data=ProfileCB(action="games").pack()
        ),
    ]])


@router.message(Command("profile"))
async def cmd_profile(message: Message, db_user) -> None:
    await message.answer(profile_text(db_user), reply_markup=profile_kb())


@router.callback_query(MenuCB.filter(F.action == "profile"))
async def cb_profile(callback: CallbackQuery, session, db_user, group) -> None:
    from bot.database.repositories.groups import GroupPlayerRepository

    await callback.answer()
    text = profile_text(db_user)
    if group is not None:
        gp = await GroupPlayerRepository(session).get_membership(group.id, db_user.id)
        if gp:
            text += "\n\n———\n\n" + profile_group_block(group, gp)
        else:
            text += "\n\n🏠 Статистики в этой группе пока нет."
    await edit_or_answer(callback, text, profile_kb())


@router.callback_query(MenuCB.filter(F.action == "settings"))
@router.callback_query(ProfileCB.filter(F.action == "back"))
async def cb_settings(callback: CallbackQuery, db_user) -> None:
    await callback.answer()
    await edit_or_answer(callback, profile_text(db_user), profile_kb())


@router.callback_query(ProfileCB.filter(F.action == "name"))
async def cb_change_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ProfileStates.display_name)
    await edit_or_answer(
        callback,
        "✏️ Пришли новое имя (до 64 символов).\n/cancel — отмена.",
        back_to_menu_kb(),
    )


@router.message(ProfileStates.display_name)
async def process_name(message: Message, state: FSMContext, session, db_user) -> None:
    name = (message.text or "").strip()[:64]
    if len(name) < 2:
        await message.answer("Имя слишком короткое. Попробуй ещё раз или /cancel.")
        return
    db_user.display_name = name
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Имя обновлено: <b>{esc(name)}</b>", reply_markup=profile_kb())


@router.callback_query(ProfileCB.filter(F.action == "games"))
async def cb_my_games(callback: CallbackQuery, session, db_user) -> None:
    await callback.answer()
    history = await GamePlayerRepository(session).history_for_user(db_user.id, 10)
    if not history:
        await edit_or_answer(callback, "📜 Сыграемых игр пока нет.", profile_kb())
        return
    from bot.roles import get_role
    from bot.database.models import WinningSide

    lines = ["📜 <b>ПОСЛЕДНИЕ ИГРЫ</b>", ""]
    for gp in history:
        game = gp.game
        role = get_role(gp.role)
        won = game.winner and gp.user_id and (
            (game.winner == WinningSide.MAFIA.value and role and role.team.value == "mafia")
            or (game.winner == WinningSide.CITY.value and role and role.team.value == "city")
            or (game.winner == WinningSide.MANIAC.value and role and role.team.value == "neutral")
        )
        icon = "✅" if won else ("🤝" if game.winner == WinningSide.DRAW.value else "❌")
        lines.append(f"{icon} Игра #{game.id} — {role.title if role else '—'}")
    await edit_or_answer(callback, "\n".join(lines), profile_kb())
