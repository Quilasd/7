"""DEBUG MODE: /testgame и /debug — тестовые игры с ботами.

Тестовые игры проходят через НАСТОЯЩИЙ движок (GameManager, PhaseManager,
NightResolver, VoteManager, RatingService, PermissionService, GroupSettings) —
отдельной игровой механики для debug не существует.

Доступ:
- глобальный Owner (OWNER_IDS) — всегда;
- остальные — право USE_DEBUG (PermissionService) + включённый debug:
  в личке — DEBUG_MODE=true, в группе — group_settings.debug_enabled.

Статистика тестовых игр меняется только при DEBUG_AFFECTS_GLOBAL_STATS /
DEBUG_AFFECTS_LOCAL_STATS (по умолчанию выключены).

Команды: /testgame [N|fast|N fast], /debug, /debug_game, /debug_state,
/debug_phase, /debug_finish_phase.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.keyboards.common import back_to_menu_kb
from bot.keyboards.testgame import test_controls_kb, test_players_count_kb
from bot.services.permissions import AdminLevel, Permission
from bot.utils.callbacks import TestCB
from bot.utils.helpers import esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()

INTRO = (
    "🧪 <b>ТЕСТОВЫЙ РЕЖИМ</b>\n\n"
    "Создаётся игра: ты + боты TestPlayerN (настоящий игровой движок).\n"
    "• роли распределятся автоматически, твоя придёт в личку;\n"
    "• боты сами выполняют ночные действия и голосуют;\n"
    "• фазы можно пропускать кнопкой; <code>/testgame fast</code> — таймеры по 5 сек;\n"
    "• состояние игры дублируется в консоль (/debug_state).\n\n"
    "Рейтинг/XP меняются только при DEBUG_AFFECTS_*_STATS=true.\n\n"
    "Выбери количество участников:"
)


async def _debug_allowed(services, session, telegram_id: int, group) -> tuple[bool, str, AdminLevel]:
    """(разрешено ли, причина, эффективный уровень)."""
    settings = get_settings()
    access = await services.permissions.resolve(
        session, telegram_id, group.id if group else None
    )
    if settings.is_owner(telegram_id):
        # Глобальный Owner может использовать debug принудительно
        return True, "", AdminLevel.OWNER
    if Permission.USE_DEBUG not in access.permissions:
        return False, "Нужен уровень Admin+ (право USE_DEBUG).", access.level
    if group is not None:
        group_settings = await services.groups.get_settings(group.id)
        if not group_settings.debug_enabled:
            return False, "В этой группе debug выключен (настройка группы).", access.level
        return True, "", access.level
    if not settings.debug_mode:
        return False, "DEBUG_MODE выключен в .env.", access.level
    return True, "", access.level


async def _guard(callback: CallbackQuery, services, session, group) -> AdminLevel | None:
    allowed, reason, level = await _debug_allowed(services, session, callback.from_user.id, group)
    if not allowed:
        await callback.answer(f"⛔️ {reason}", show_alert=True)
        return None
    return level


def _parse_args(args: str | None) -> tuple[int | None, bool]:
    """'/testgame 6 fast' -> (6, True); 'fast' -> (None, True)."""
    count = None
    fast = False
    for token in (args or "").lower().split():
        if token.isdigit():
            count = int(token)
        elif token in ("fast", "f", "быстро"):
            fast = True
    return count, fast


@router.message(Command("testgame"))
async def cmd_testgame(message: Message, command=None, session=None, group=None, services=None, db_user=None) -> None:
    if services is None:
        # вызов из другого хендлера передаёт все зависимости явно
        return
    args = command.args if command else None
    allowed, reason, _ = await _debug_allowed(services, session, message.from_user.id, group)
    if not allowed:
        await message.answer(f"⛔️ {esc(reason)}")
        return

    count, fast = _parse_args(args)
    if count is None and not fast:
        await message.answer(INTRO, reply_markup=test_players_count_kb())
        return
    count = count or 5
    await _create(message, count, fast, services, db_user, group)


async def _create(event, players_count: int, fast: bool, services, db_user, group=None) -> None:
    game_id, message_text = await services.test_games.create_test_game(
        db_user.id,
        players_count,
        fast=fast,
        group_id=group.id if group else None,
    )
    if game_id is None:
        text = f"❌ {esc(message_text)}"
        if isinstance(event, CallbackQuery):
            await event.answer(message_text[:180], show_alert=True)
            await edit_or_answer(event, text, test_players_count_kb())
        else:
            await event.answer(text, reply_markup=test_players_count_kb())
        return
    await services.audit.log(
        db_user.id, "testgame", None, group.id if group else None,
        f"game={game_id} players={players_count} fast={fast}",
    )
    suffix = " ⚡️ fast (таймеры по 5 сек)" if fast else ""
    text = (
        f"{esc(message_text)}{suffix}\n\n"
        f"🎭 Твоя роль придёт отдельным сообщением.\n"
        f"Играй сам или предоставь всё ботам — авто-действия уже включены."
    )
    keyboard = test_controls_kb(game_id, auto_on=True)
    if isinstance(event, CallbackQuery):
        await event.answer("Создана!")
        await edit_or_answer(event, text, keyboard)
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(TestCB.filter(F.action == "create"))
async def cb_test_create(
    callback: CallbackQuery, callback_data: TestCB, services, session, db_user, group
) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    await _create(callback, int(callback_data.value), False, services, db_user, group)


@router.callback_query(TestCB.filter(F.action == "toadmin"))
async def cb_to_admin(callback: CallbackQuery) -> None:
    from bot.keyboards.admin import admin_panel_kb

    await callback.answer()
    await edit_or_answer(callback, "🛠 <b>АДМИН-ПАНЕЛЬ</b>", admin_panel_kb(debug_mode=get_settings().debug_mode))


@router.callback_query(TestCB.filter(F.action == "status"))
async def cb_test_status(callback: CallbackQuery, callback_data: TestCB, services, session, group) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    game_id = int(callback_data.value)
    text = await services.test_games.dump_state(game_id)
    auto_on = services.test_games.auto_is_on(game_id)
    await callback.answer()
    await edit_or_answer(callback, text, test_controls_kb(game_id, auto_on))


@router.callback_query(TestCB.filter(F.action == "skip"))
async def cb_test_skip(callback: CallbackQuery, callback_data: TestCB, services, session, group) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    game_id = int(callback_data.value)
    result = await services.test_games.skip_phase(game_id)
    await services.audit.log(
        callback.from_user.id, "debug_skip_phase", None, group.id if group else None, f"game={game_id}"
    )
    await callback.answer(result[:180], show_alert=False)
    text = await services.test_games.dump_state(game_id)
    await edit_or_answer(
        callback, f"{esc(result)}\n\n{text}",
        test_controls_kb(game_id, services.test_games.auto_is_on(game_id)),
    )


@router.callback_query(TestCB.filter(F.action == "actnow"))
async def cb_test_actnow(callback: CallbackQuery, callback_data: TestCB, services, session, group) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    game_id = int(callback_data.value)
    result = await services.test_games.act_now(game_id)
    await callback.answer(result[:180])
    await edit_or_answer(
        callback, esc(result),
        test_controls_kb(game_id, services.test_games.auto_is_on(game_id)),
    )


@router.callback_query(TestCB.filter(F.action == "auto"))
async def cb_test_auto(callback: CallbackQuery, callback_data: TestCB, services, session, group) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    game_id = int(callback_data.value)
    state = services.test_games.toggle_auto(game_id)
    if state is None:
        services.test_games.start_supervisor(game_id)
        state = True
        answer = "🟢 Авто-действия ботов включены"
    else:
        answer = "🟢 Авто ботов: ВКЛ" if state else "🔴 Авто ботов: ВЫКЛ"
    await callback.answer(answer)
    await edit_or_answer(
        callback, f"{answer}\n\n🧪 Тест-игра #{game_id}", test_controls_kb(game_id, state)
    )


@router.callback_query(TestCB.filter(F.action == "finish"))
async def cb_test_finish(callback: CallbackQuery, callback_data: TestCB, services, session, group, db_user) -> None:
    if await _guard(callback, services, session, group) is None:
        return
    game_id = int(callback_data.value)
    result = await services.test_games.finish(game_id)
    await services.audit.log(
        db_user.id, "debug_finish", None, group.id if group else None, f"game={game_id}"
    )
    await callback.answer(result[:180], show_alert=True)
    await edit_or_answer(callback, esc(result), back_to_menu_kb())


# ------------------------------------------------------------- /debug

@router.message(Command("debug", "debug_game", "debug_state", "debug_phase", "debug_finish_phase"))
async def cmd_debug(message: Message, command, session, group, services, db_user) -> None:
    allowed, reason, level = await _debug_allowed(services, session, message.from_user.id, group)
    if not allowed:
        await message.answer(f"⛔️ {esc(reason)}")
        return

    name = command.command
    if name == "debug":
        settings = get_settings()
        flags = (
            f"DEBUG_MODE={'true' if settings.debug_mode else 'false'}\n"
            f"DEBUG_AFFECTS_GLOBAL_STATS={'true' if settings.debug_affects_global_stats else 'false'}\n"
            f"DEBUG_AFFECTS_LOCAL_STATS={'true' if settings.debug_affects_local_stats else 'false'}"
        )
        group_line = "—"
        if group is not None:
            group_settings = await services.groups.get_settings(group.id)
            group_line = f"{group.title or group.telegram_chat_id}: debug_enabled={group_settings.debug_enabled}"
        await message.answer(
            "🧪 <b>DEBUG MODE</b>\n\n"
            f"<code>{flags}</code>\n\n"
            f"Группа: {esc(group_line)}\n"
            f"Твой уровень: <b>{level.name}</b>\n\n"
            "Команды: /testgame [N|fast], /debug_state (дамп игры),\n"
            "/debug_phase или /debug_finish_phase (пропуск фазы), /debug_game (список тест-игр)."
        )
        return

    if name == "debug_game":
        ids = services.test_games.supervised_games()
        if not ids:
            await message.answer("Активных тест-игр нет. Создай: /testgame 5 fast")
        else:
            await message.answer("🧪 Активные тест-игры: " + ", ".join(f"#{gid}" for gid in ids))
        return

    args = (command.args or "").strip()
    game_id = int(args) if args.isdigit() else None
    if game_id is None:
        active = services.test_games.supervised_games()
        game_id = active[0] if active else None
    if game_id is None:
        await message.answer("Укажи ID игры: /debug_state 12")
        return

    if name in ("debug_state", "debug_game"):
        text = await services.test_games.dump_state(game_id)
        await message.answer(text)
        return

    # debug_phase / debug_finish_phase — пропуск текущей фазы
    result = await services.test_games.skip_phase(game_id)
    await services.audit.log(
        db_user.id, "debug_skip_phase", None, group.id if group else None, f"game={game_id}"
    )
    await message.answer(esc(result))
