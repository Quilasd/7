"""Первоначальная настройка сервера (группы): onboarding + /setup.

Три механизма, НЕ затрагивающие /start (он остаётся игровым меню):
- автоматическое сообщение при добавлении бота в группу (my_chat_member);
- /setup — ручная первичная настройка/проверка (идемпотентна);
- /settings — дальнейшие настройки уже подключённого сервера (groups_admin).

Доступ к настройке: Telegram-администраторы текущей группы (creator/
administrator) + локальные Senior Admin+ (MANAGE_SETTINGS) + глобальный
Owner — совместимо с существующей системой прав.

Multi-server: всё привязано к chat_id (groups.telegram_chat_id — ключ);
повторное добавление бота не создаёт дублей (get_or_create идемпотентен).
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.repositories.groups import GroupRepository, GroupSettingsRepository
from bot.utils.callbacks import SetupCB
from bot.utils.helpers import esc

logger = logging.getLogger(__name__)

router = Router(name="setup")


WELCOME_TEXT = (
    "🤖 <b>Mafia Online добавлен!</b>\n\n"
    "Чтобы бот мог полноценно работать в этой группе, "
    "администратору необходимо:\n\n"
    "1️⃣ Выдать боту права администратора.\n"
    "2️⃣ Включить в группе режим форума / «Темы».\n"
    "3️⃣ Выдать боту право «Управление темами».\n"
    "4️⃣ После этого выполнить /setup.\n\n"
    "Игры, профиль и рейтинги доступны и сейчас — просто напишите /start."
)


def _check_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔧 Проверить настройку",
            callback_data=SetupCB(action="check").pack(),
        )
    ]])


# ------------------------------------------------------------ права доступа


async def is_group_setup_admin(bot: Bot, session, services, group, user) -> bool:
    """Может ли пользователь настраивать сервер этой группы.

    Telegram-администратор группы (creator/administrator) ИЛИ локальный
    Senior Admin+ (MANAGE_SETTINGS) ИЛИ глобальный Owner. Правила одной
    группы не действуют в другой.
    """
    if group is None or user is None:
        return False
    settings = services.settings
    if settings is not None and settings.is_owner(user.telegram_id):
        return True
    # локальный уровень (права других групп не учитываются)
    from bot.services.permissions import Permission

    access = await services.permissions.resolve(
        session, user.telegram_id, group.id
    )
    if Permission.MANAGE_SETTINGS in access.permissions:
        return True
    # Telegram-права в ЭТОМ чате
    try:
        member = await bot.get_chat_member(group.telegram_chat_id, user.telegram_id)
        return getattr(member, "status", "") in ("creator", "administrator")
    except Exception as exc:
        logger.warning("Setup: get_chat_member(%s, %s): %s",
                       group.telegram_chat_id, user.telegram_id, exc)
        return False


# ------------------------------------------------------------ отчёты


def _render_report(check, group_title: str) -> str:
    """Текст отчёта проверки: успех — сводка, провал — конкретные проблемы."""
    if check.ok:
        lines = [
            "✅ <b>Mafia Online успешно настроен!</b>", "",
            f"Группа: <b>{esc(group_title or check.title or '—')}</b>", "",
            "Статус:",
            "🟢 Бот — администратор",
            "🟢 Управление темами — доступно",
            "🟢 Forum Topics — включены",
            "🟢 База данных — подключена",
        ]
        return "\n".join(lines)

    lines = ["⚙️ <b>Проверка настройки Mafia Online</b>", ""]
    if check.problems:
        for emoji, text in check.problems:
            lines.append(f"{emoji} {text}")
            lines.append("")
    else:
        lines.append("❌ Не все проверки пройдены. Повторите /setup.")
    lines.append("После исправления выполните /setup ещё раз.")
    return "\n".join(lines)


async def _run_setup_check(bot: Bot, session, services, chat_id: int) -> tuple:
    """Полная проверка + (если ок) применение настройки. Возвращает (check, ok)."""
    check = await services.setup.check(bot, chat_id)
    if check.ok:
        await services.setup.apply(bot, chat_id, title=check.title)
        logger.info("Setup: группа %s настроена успешно", chat_id)
    return check, check.ok


# ------------------------------------------------------------ /setup


@router.message(Command("setup"))
async def cmd_setup(message: Message, session, services, db_user, group=None, bot: Bot = None) -> None:
    """Ручная первичная настройка/проверка сервера (идемпотентна)."""
    if message.chat.type not in ("group", "supergroup"):
        await message.answer(
            "⚙️ /setup настраивает сервер — выполните команду в группе, "
            "куда добавлен Mafia Online."
        )
        return

    # группа в БД — идемпотентно (обычно уже создана GroupContextMiddleware)
    if group is None:
        group = await GroupRepository(session).get_or_create(
            message.chat.id, getattr(message.chat, "title", "") or ""
        )
        await session.commit()  # зафиксировать до записей в других сессиях

    if not await is_group_setup_admin(bot, session, services, group, db_user):
        await message.answer("❌ У вас нет прав для настройки Mafia Online.")
        return

    check, _ = await _run_setup_check(bot, session, services, message.chat.id)
    await message.answer(_render_report(check, group.title or check.title))


def _autocheck_lines(check) -> str:
    """Результат автоматической проверки после добавления (ТЗ §2).

    Конкретная причина по приоритету: админ → право тем → сами темы;
    при полном порядке — «готов к работе» с предложением /setup.
    """
    if not check.is_admin:
        return (
            "❌ Бот пока не может работать.\n\n"
            "Необходимо выдать Mafia Online права администратора.\n"
            "После выдачи прав повторите /setup."
        )
    if not check.can_manage_topics:
        return (
            "❌ Не хватает права «Управление темами».\n\n"
            "Выдайте боту право на управление темами и повторите /setup."
        )
    if not check.is_forum:
        return (
            "❌ В этой группе не включены темы форума.\n\n"
            "Включите «Темы» в настройках группы и повторите /setup."
        )
    return (
        "✅ Mafia Online готов к работе в этой группе!\n\n"
        "Можно выполнить /setup для завершения настройки."
    )


# ------------------------------------------------------------ onboarding


@router.my_chat_member()
async def on_bot_added(event: ChatMemberUpdated, bot: Bot, session, services) -> None:
    """Добавление/удаление бота в группе: приветствие и первичный статус.

    Идемпотентно: группа в БД не дублируется (get_or_create по chat_id);
    повторное добавление показывает текущий статус и предлагает /setup.
    """
    new_status = event.new_chat_member.status if event.new_chat_member else ""
    chat_id = event.chat.id
    chat_title = event.chat.title or ""

    if new_status in ("kicked", "left"):
        logger.info("Бот удалён из группы %s (%s)", chat_id, chat_title)
        # данные группы сохраняются — при повторном добавлении настройка
        # продолжится с того же места, без дублей
        return

    if new_status not in ("member", "administrator"):
        return

    # гарантируем запись группы без дублей (обычно уже создана middleware)
    async with services.session_factory() as s:
        group = await GroupRepository(s).get_or_create(chat_id, chat_title)
        settings = await GroupSettingsRepository(s).get_for(group.id)
        was_setup = bool(
            settings is not None and settings.setup_completed_at is not None
        )
        await s.commit()

    try:
        # автоматическая проверка текущего состояния (ТЗ §2)
        check = await services.setup.check(bot, chat_id)
        if was_setup:
            # повторное добавление уже настроенной группы: актуальный статус
            text = (
                "🤖 <b>Mafia Online снова в группе!</b>\n\n"
                "Настройки этой группы сохранены.\n\n"
                + _render_report(check, chat_title)
            )
        else:
            # первичное добавление: инструкция + результат автопроверки
            text = (
                WELCOME_TEXT
                + "\n\n⚙️ <b>Автоматическая проверка:</b>\n\n"
                + _autocheck_lines(check)
            )
        await bot.send_message(chat_id, text, reply_markup=_check_kb())
        logger.info("Onboarding: сообщение отправлено в группу %s", chat_id)
    except Exception as exc:
        # бот мог не успеть получить права на отправку сообщений — это норма
        logger.warning("Onboarding: не удалось отправить сообщение в %s: %s", chat_id, exc)


# ------------------------------------------------------------ кнопка


@router.callback_query(SetupCB.filter(F.action == "check"))
async def cb_setup_check(
    callback: CallbackQuery, session, services, db_user, group=None, bot: Bot = None
) -> None:
    """Кнопка «🔧 Проверить настройку»: та же проверка, что и /setup."""
    if group is None:
        await callback.answer(
            "⚙️ Проверка доступна в группе, где добавлен Mafia Online.",
            show_alert=True,
        )
        return
    if not await is_group_setup_admin(bot, session, services, group, db_user):
        await callback.answer(
            "❌ Настройку сервера может выполнять только администратор.",
            show_alert=True,
        )
        return

    check, _ = await _run_setup_check(bot, session, services, group.telegram_chat_id)
    text = _render_report(check, group.title)
    # обновляем исходное сообщение — без мусора в чате
    try:
        await callback.message.edit_text(
            text, reply_markup=_check_kb()
        )
        await callback.answer()
    except Exception:
        # сообщение не редактируется (устарело/удалено) — отвечаем алертом
        await callback.answer("Готово: проверка выполнена. Повторите /setup при изменениях.")
