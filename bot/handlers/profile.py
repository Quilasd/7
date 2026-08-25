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


def profile_text(user, header: bool = True, progression=None, ranks=None, extras=None) -> str:
    """Глобальный блок профиля (статистика User, не зависит от групп).

    ranks: {'rating': int, 'wins': int, 'level': int} — позиции в рейтинге.
    extras: {'title': str, 'event_reward': str, 'achievements': 'X/Y',
             'win_streak': int, 'best_win_streak': int} — доп. блоки.
    """
    from bot.services.progression import DEFAULT_PROGRESSION

    progression = progression or DEFAULT_PROGRESSION
    ranks = ranks or {}
    extras = extras or {}
    total = user.wins + user.losses
    winrate = (user.wins / total * 100) if total else 0.0
    _, in_level, need = progression.xp_progress_in_level(user.xp)

    def _rank(key: str) -> str:
        pos = ranks.get(key)
        return f" (#{pos})" if pos else ""

    lines = ["🌐 <b>ГЛОБАЛЬНО</b>", ""]
    # компактный рейтинг-блок: общий/победы/уровень с позициями в топе
    lines.append(f"⭐ Общий: <b>{user.rating}</b>{_rank('rating')}")
    lines.append(f"🏆 Побед: <b>{user.wins}</b>{_rank('wins')}")
    lines.append(f"📈 Уровень: <b>{user.level}</b>{_rank('level')}")
    streak = extras.get("win_streak", 0)
    best = extras.get("best_win_streak", 0)
    lines.append(f"🔥 Серия побед: <b>{streak}</b> / 🏆 Лучшая серия: <b>{best}</b>")
    badge_lines = []
    if extras.get("title"):
        badge_lines.append(f"🎓 Титул: {extras['title']}")
    if extras.get("event_reward"):
        badge_lines.append(f"🎪 Награда: {extras['event_reward']}")
    if extras.get("achievements"):
        badge_lines.append(f"🏅 Достижения: {extras['achievements']}")
    if badge_lines:
        lines.append("\n".join(badge_lines))
    lines += [
        "",
        f"✨ XP: <b>{user.xp}</b> (до следующего уровня {in_level}/{need})",
        f"💀 Поражений: <b>{user.losses}</b>  ·  🎮 Игр: <b>{user.games_played}</b>  ·  📈 Winrate: <b>{winrate:.0f}%</b>",
        "",
        f"☠️ Убийств: <b>{user.kills}</b>  ·  ❤️ Спасений: <b>{user.saves}</b>",
        f"🕵️ Расследований: <b>{user.investigations}</b>  ·  🗳 Верный голос: <b>{user.correct_votes}</b>",
    ]
    if header:
        head = [
            f"👤 <b>{esc(display_name(user))}</b>",
            f"ID: <code>{user.telegram_id}</code>",
            "",
        ]
        lines = head + lines
    return "\n".join(lines)


def profile_group_block(group, group_player, progression=None, ranks=None) -> str:
    """Локальный блок: статистика игрока В КОНКРЕТНОЙ группе (GroupPlayer).

    ranks: {'rating': int, 'wins': int, 'level': int} — позиции в топе группы.
    """
    from bot.services.progression import DEFAULT_PROGRESSION

    progression = progression or DEFAULT_PROGRESSION
    ranks = ranks or {}
    gp = group_player
    total = gp.wins + gp.losses
    winrate = (gp.wins / total * 100) if total else 0.0
    _, in_level, need = progression.xp_progress_in_level(gp.xp)
    streak = getattr(gp, "win_streak", 0) or 0
    best = getattr(gp, "best_win_streak", 0) or 0

    def _rank(key: str) -> str:
        pos = ranks.get(key)
        return f" (#{pos})" if pos else ""

    return "\n".join([
        f"🏠 <b>ЭТА ГРУППА</b>\n<i>{esc(group.title or '')}</i>\n",
        f"⭐ Общий: <b>{gp.rating}</b>{_rank('rating')}",
        f"🏆 Побед: <b>{gp.wins}</b>{_rank('wins')}",
        f"📈 Уровень: <b>{gp.level}</b>{_rank('level')}",
        f"🔥 Серия: <b>{streak}</b> / 🏆 Лучшая: <b>{best}</b>",
        f"✨ XP: <b>{gp.xp}</b> (до следующего уровня {in_level}/{need})",
        f"💀 Поражений: <b>{gp.losses}</b>  ·  🎮 Игр: <b>{gp.games_played}</b>  ·  📈 Winrate: <b>{winrate:.0f}%</b>",
        "",
        f"☠️ Убийств: <b>{gp.kills}</b>  ·  ❤️ Спасений: <b>{gp.saves}</b>",
        f"🕵️ Расследований: <b>{gp.investigations}</b>  ·  🗳 Верный голос: <b>{gp.correct_votes}</b>",
    ])


async def compute_profile_extras(session, user, services) -> dict:
    """Ранги (глобальные) + титул/награда/достижения/серия для блока профиля."""
    from bot.database.repositories.social import UserAchievementRepository
    from bot.database.repositories.users import UserRepository
    from bot.services import achievements as ach, titles as ttl

    users = UserRepository(session)
    ranks = {
        "rating": await users.rank_by_rating(user.rating),
        "wins": await users.rank_by_wins(user.wins),
        "level": await users.rank_by_level(user.level, user.xp),
    }
    count = await UserAchievementRepository(session).count(user.id)
    extras = {
        "title": ttl.title_display(user.active_title),
        "event_reward": await services.rewards.active_display(session, user),
        "achievements": f"{count}/{ach.total_achievements()}",
        "win_streak": int(getattr(user, "win_streak", 0) or 0),
        "best_win_streak": int(getattr(user, "best_win_streak", 0) or 0),
    }
    return {"ranks": ranks, "extras": extras}


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
async def cmd_profile(message: Message, session, services, db_user) -> None:
    data = await compute_profile_extras(session, db_user, services)
    await message.answer(
        profile_text(db_user, ranks=data["ranks"], extras=data["extras"]), reply_markup=profile_kb()
    )


@router.callback_query(MenuCB.filter(F.action == "profile"))
async def cb_profile(callback: CallbackQuery, session, services, db_user, group) -> None:
    from bot.database.repositories.groups import GroupPlayerRepository

    await callback.answer()
    data = await compute_profile_extras(session, db_user, services)
    text = profile_text(db_user, ranks=data["ranks"], extras=data["extras"])
    if group is not None:
        gp = await GroupPlayerRepository(session).get_membership(group.id, db_user.id)
        if gp:
            # локальные позиции в топе этой группы (существующая система рейтингов)
            group_repo = GroupPlayerRepository(session)
            local_ranks = {
                "rating": await group_repo.rank_in_group(group.id, "rating", gp.rating),
                "wins": await group_repo.rank_in_group(group.id, "wins", gp.wins),
                "level": await group_repo.rank_in_group(group.id, "level", gp.level, gp.xp),
            }
            text += "\n\n———\n\n" + profile_group_block(group, gp, ranks=local_ranks)
        else:
            text += "\n\n🏠 Статистики в этой группе пока нет."
    await edit_or_answer(callback, text, profile_kb())


@router.callback_query(MenuCB.filter(F.action == "settings"))
@router.callback_query(ProfileCB.filter(F.action == "back"))
async def cb_settings(callback: CallbackQuery, session, services, db_user) -> None:
    await callback.answer()
    data = await compute_profile_extras(session, db_user, services)
    await edit_or_answer(
        callback, profile_text(db_user, ranks=data["ranks"], extras=data["extras"]), profile_kb()
    )


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
