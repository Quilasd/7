"""Стартовый хендлер, главное меню, правила, рейтинг, список комнат."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.repositories.games import GamePlayerRepository
from bot.database.repositories.rooms import RoomRepository
from bot.keyboards.common import back_to_menu_kb, main_menu_kb
from bot.keyboards.room import rooms_list_kb
from bot.utils.callbacks import GameCB, MenuCB
from bot.utils.helpers import display_name, esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()

WELCOME = (
    "🎭 <b>МАФИЯ ОНЛАЙН</b>\n\n"
    "Привет, {name}! Это полноценная «Мафия» прямо в Telegram:\n"
    "• комнаты на 4–20 игроков, приватные и публичные\n"
    "• роли: мафия, комиссар, доктор, маньяк, любовница, телохранитель\n"
    "• ночи, дни, голосования и честная статистика\n\n"
    "Выбери действие 👇"
)

RULES = (
    "📖 <b>ПРАВИЛА ИГРЫ</b>\n\n"
    "🎬 <b>Старт.</b> Игроки собираются в комнате, отмечаются готовыми, "
    "создатель запускает игру. Роли распределяются случайно и приходят в личку.\n\n"
    "🌙 <b>Ночь.</b> Мафия выбирает жертву, доктор лечит, комиссар проверяет, "
    "любовница блокирует, телохранитель защищает, маньяк убивает. "
    "Порядок обработки: блокировка → защита → убийство → проверка.\n\n"
    "☀️ <b>День.</b> Город обсуждает произошедшее.\n\n"
    "🗳 <b>Голосование.</b> Живые игроки изгоняют одного подозреваемого. "
    "При ничьей — либо повторное голосование, либо никто не умирает "
    "(настройка комнаты).\n\n"
    "🏆 <b>Победа.</b>\n"
    "• 🔴 Мафия побеждает, когда мафии не меньше, чем всех остальных живых.\n"
    "• 🔵 Мирные побеждают, когда вся мафия уничтожена.\n"
    "• 🔪 Маньяк побеждает, если остаётся одним из двух последних.\n\n"
    "Роли:\n"
    "🔴 <b>Мафия</b> — знает союзников, ночью убивает.\n"
    "🔵 <b>Мирный</b> — голосует и рассуждает.\n"
    "🕵️ <b>Комиссар</b> — ночью проверяет игрока на мафию.\n"
    "❤️ <b>Доктор</b> — спасает от убийства (не лечит одну цель дважды подряд).\n"
    "🔪 <b>Маньяк</b> — сам за себя, убивает каждую ночь.\n"
    "👰 <b>Любовница</b> — блокирует ночное действие игрока.\n"
    "🛡 <b>Телохранитель</b> — спасает цель ценой своей жизни.\n"
)


@router.message(CommandStart())
async def cmd_start(message: Message, db_user) -> None:
    await message.answer(
        WELCOME.format(name=esc(display_name(db_user))),
        reply_markup=main_menu_kb(),
    )
    # Telegram позволяет BotCommandScopeChat только после начала диалога:
    # если админ из .env написал боту впервые — доставляем его расширенное
    # меню команд именно сейчас (на старте бота это могло быть невозможно).
    from bot.config import get_settings
    from bot.utils.commands_menu import refresh_user_commands

    settings = get_settings()
    if settings.is_admin(message.from_user.id) or settings.is_owner(message.from_user.id):
        await refresh_user_commands(
            message.bot,
            message.from_user.id,
            is_admin=settings.is_admin(message.from_user.id),
            is_owner=settings.is_owner(message.from_user.id),
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(RULES, reply_markup=back_to_menu_kb())


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main(callback: CallbackQuery) -> None:
    await edit_or_answer(callback, "🎭 <b>Главное меню</b>\n\nВыбери действие 👇", main_menu_kb())


@router.callback_query(MenuCB.filter(F.action == "rules"))
async def cb_rules(callback: CallbackQuery) -> None:
    await edit_or_answer(callback, RULES, back_to_menu_kb())


# Кнопка 📊 Рейтинг обрабатывается в ratings.py (cb_menu_rating): роутер ratings
# подключается РАНЬДЕ start, поэтому обработчик здесь был недостижимым дубликатом
# и удалён во избежание конфликтов callback_data.


def _active_game_kb(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎮 Состояние игры",
            callback_data=GameCB(action="status", game_id=game_id).pack(),
        )
    ]])


@router.callback_query(MenuCB.filter(F.action == "play"))
@router.callback_query(MenuCB.filter(F.action == "find"))
async def cb_play(callback: CallbackQuery, session, db_user, group=None) -> None:
    await callback.answer()
    players = GamePlayerRepository(session)

    active_game_gp = await players.active_game_of_user(db_user.id)
    if active_game_gp:
        await edit_or_answer(
            callback,
            "🎮 У тебя уже идёт игра! Открой её состояние:",
            _active_game_kb(active_game_gp.game_id),
        )
        return

    rooms = RoomRepository(session)
    # Изоляция групп (ТЗ): в группе — только комнаты ЭТОЙ группы,
    # в ЛС — только глобальные комнаты (без группы).
    if group is not None:
        open_rooms = await rooms.for_group(group.id)
    else:
        open_rooms = await rooms.open_public_rooms(10)
    my_room = await rooms.open_room_of_user(db_user.id)
    if not open_rooms and my_room is None:
        await edit_or_answer(
            callback,
            (
                f"🔎 Открытых комнат в группе «{esc(group.title or '')}» нет."
                if group is not None else
                "🔎 Открытых публичных комнат нет.\n\nСоздай свою — «🏠 Создать комнату»!"
            ),
            back_to_menu_kb(),
        )
        return

    if group is not None:
        title = f"🔎 <b>КОМНАТЫ ГРУППЫ</b> · <i>{esc(group.title or '')}</i>"
    else:
        title = "🔎 <b>ОТКРЫТЫЕ КОМНАТЫ</b>"
    lines = [title, ""]
    for room in open_rooms:
        lock = "🔐" if room.is_private else "🌍"
        lines.append(
            f"• #{room.id} «{esc(room.name)}» {lock} — 👥 {room.player_count()}/{room.max_players}"
        )
    lines.append("")
    lines.append("Нажми на комнату ниже, чтобы открыть её 👇")
    await edit_or_answer(callback, "\n".join(lines), rooms_list_kb(open_rooms, my_room))
