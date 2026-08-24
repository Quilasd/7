"""DEBUG MODE: /testgame — тестовые игры с ботами (только для ADMIN_IDS + DEBUG_MODE).

Боты (TestPlayerN) автоматически выполняют ночные действия и голосуют,
админ наблюдает полный игровой цикл в своей личке и управляет им кнопками.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import get_settings
from bot.keyboards.common import back_to_menu_kb
from bot.keyboards.testgame import test_controls_kb, test_players_count_kb
from bot.utils.callbacks import TestCB
from bot.utils.helpers import esc
from bot.utils.telegram import edit_or_answer

logger = logging.getLogger(__name__)
router = Router()

INTRO = (
    "🧪 <b>ТЕСТОВЫЙ РЕЖИМ</b>\n\n"
    "Создаётся игра: ты + боты TestPlayerN.\n"
    "• роли распределятся автоматически, твоя придёт в личку;\n"
    "• боты сами выполняют ночные действия и голосуют;\n"
    "• тайминги ускоренные (6 сек на фазу), фазы можно пропускать кнопкой;\n"
    "• состояние игры дублируется в консоль.\n\n"
    "Рейтинг и статистика в тестовых играх не меняются.\n\n"
    "Выбери количество участников:"
)


def _allowed(user_id: int) -> bool:
    settings = get_settings()
    return settings.is_admin(user_id) and settings.debug_mode


async def _guard(callback: CallbackQuery) -> bool:
    if not _allowed(callback.from_user.id):
        await callback.answer("⛔️ Недоступно (нужен ADMIN_IDS + DEBUG_MODE=true)", show_alert=True)
        return False
    return True


@router.message(Command("testgame"))
async def cmd_testgame(message: Message) -> None:
    if not _allowed(message.from_user.id):
        await message.answer("⛔️ Команда доступна только администраторам при DEBUG_MODE=true.")
        return
    args = (message.text or "").split()
    if len(args) > 1 and args[1].isdigit():
        # /testgame 5 — сразу создать игру на 5 участников
        await _create(message, int(args[1]))
        return
    await message.answer(INTRO, reply_markup=test_players_count_kb())


async def _create(event, players_count: int, services, db_user) -> None:
    game_id, message_text = await services.test_games.create_test_game(
        db_user.id, players_count
    )
    if game_id is None:
        text = f"❌ {esc(message_text)}"
        if isinstance(event, CallbackQuery):
            await event.answer(message_text[:180], show_alert=True)
            await edit_or_answer(event, text, test_players_count_kb())
        else:
            await event.answer(text, reply_markup=test_players_count_kb())
        return
    text = (
        f"{esc(message_text)}\n\n"
        f"🎭 Твоя роль придёт отдельным сообщением.\n"
        f"Играй сам или предоставь всё ботам — авто-действия уже включены "
        f"(⏱ фазы по 6 сек, счёт в консоли)."
    )
    keyboard = test_controls_kb(game_id, auto_on=True)
    if isinstance(event, CallbackQuery):
        await event.answer("Создана!")
        await edit_or_answer(event, text, keyboard)
    else:
        await event.answer(text, reply_markup=keyboard)


@router.callback_query(TestCB.filter(F.action == "create"))
async def cb_test_create(
    callback: CallbackQuery, callback_data: TestCB, services, db_user
) -> None:
    if not await _guard(callback):
        return
    await _create(callback, int(callback_data.value), services, db_user)


@router.callback_query(TestCB.filter(F.action == "toadmin"))
async def cb_to_admin(callback: CallbackQuery) -> None:
    if not await _guard(callback):
        return
    from bot.keyboards.admin import admin_panel_kb

    await callback.answer()
    await edit_or_answer(callback, "🛠 <b>АДМИН-ПАНЕЛЬ</b>", admin_panel_kb())


@router.callback_query(TestCB.filter(F.action == "status"))
async def cb_test_status(callback: CallbackQuery, callback_data: TestCB, services) -> None:
    if not await _guard(callback):
        return
    game_id = int(callback_data.value)
    text = await services.test_games.dump_state(game_id)
    auto_on = services.test_games.auto_is_on(game_id)
    await callback.answer()
    await edit_or_answer(callback, text, test_controls_kb(game_id, auto_on))


@router.callback_query(TestCB.filter(F.action == "skip"))
async def cb_test_skip(callback: CallbackQuery, callback_data: TestCB, services) -> None:
    if not await _guard(callback):
        return
    game_id = int(callback_data.value)
    result = await services.test_games.skip_phase(game_id)
    await callback.answer(result[:180], show_alert=False)
    text = await services.test_games.dump_state(game_id)
    await edit_or_answer(callback, f"{esc(result)}\n\n{text}", test_controls_kb(game_id, services.test_games.auto_is_on(game_id)))


@router.callback_query(TestCB.filter(F.action == "actnow"))
async def cb_test_actnow(callback: CallbackQuery, callback_data: TestCB, services) -> None:
    if not await _guard(callback):
        return
    game_id = int(callback_data.value)
    result = await services.test_games.act_now(game_id)
    await callback.answer(result[:180])
    await edit_or_answer(
        callback, esc(result), test_controls_kb(game_id, services.test_games.auto_is_on(game_id))
    )


@router.callback_query(TestCB.filter(F.action == "auto"))
async def cb_test_auto(callback: CallbackQuery, callback_data: TestCB, services) -> None:
    if not await _guard(callback):
        return
    game_id = int(callback_data.value)
    state = services.test_games.toggle_auto(game_id)
    if state is None:
        # супервизор не запущен (например, после рестарта) — поднимаем заново
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
async def cb_test_finish(callback: CallbackQuery, callback_data: TestCB, services) -> None:
    if not await _guard(callback):
        return
    game_id = int(callback_data.value)
    result = await services.test_games.finish(game_id)
    await callback.answer(result[:180], show_alert=True)
    await edit_or_answer(callback, esc(result), back_to_menu_kb())
