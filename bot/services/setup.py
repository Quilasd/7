"""Первичная настройка сервера (Telegram-группы): проверки для /setup.

GroupSetupService проверяет готовность группы к полноценной работе Mafia
Online и применяет настройку идемпотентно:

Проверки (по ТЗ «Настройка сервера»):
1. чат — группа/супергруппа;
2. бот — администратор группы;
3. у бота есть право can_manage_topics;
4. в группе включены Forum Topics (чат является форумом);
5. группа есть в БД (groups, ключ — telegram_chat_id = chat_id);
6. настройки группы (group_settings) корректно сохранены.

Применение (apply) при успешной проверке:
- get_or_create группы и настроек (БЕЗ дублирования — повторный /setup
  ничего не создаёт заново);
- если группа является форумом и у неё ещё не настроены форумы партий —
  автозаполнение game_forum_chat_id/mafia_forum_chat_id = chat_id группы
  (темы партий создаются прямо в этой группе; при желании владелец
  переназначает их командой /set_game_forum);
- фиксация setup_completed_at.

Все Telegram-вызовы устойчивы к ошибкам: сбой API не роняет бота —
превращается в понятную проблему в отчёте.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.repositories.groups import (
    GroupRepository,
    GroupSettingsRepository,
)
from bot.utils.helpers import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SetupCheck:
    """Снимок готовности группы (все поля — человекочитаемые статусы)."""

    chat_type_ok: bool = False
    is_admin: bool = False
    can_manage_topics: bool = False
    is_forum: bool = False
    in_db: bool = False
    settings_ok: bool = False
    title: str = ""
    # список (emoji, строка) для отчёта; пустой = всё хорошо
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Полная готовность: Telegram-условия выполнены.

        in_db/settings_ok — информационные: запись группы и настроек
        создаётся применением настройки (apply) автоматически, их отсутствие
        не является проблемой владельца группы.
        """
        return (
            not self.problems
            and self.chat_type_ok
            and self.is_admin
            and self.can_manage_topics
            and self.is_forum
        )


class GroupSetupService:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------ проверка

    async def check(self, bot, chat_id: int) -> SetupCheck:
        """Проверить текущее состояние группы (только чтение, без записи)."""
        check = SetupCheck()

        # 1. тип чата
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as exc:
            logger.warning("Setup: get_chat(%s) недоступен: %s", chat_id, exc)
            check.problems.append(
                ("❌", "Не удалось получить информацию о группе: возможно, бот удалён "
                      "из неё. Добавьте бота обратно и повторите /setup.")
            )
            return check
        check.title = chat.title or ""
        check.chat_type_ok = chat.type in ("group", "supergroup")
        if not check.chat_type_ok:
            check.problems.append((
                "❌", "Настройка доступна только в группах и супергруппах — "
                      "эта команда не для личного чата."
            ))
            return check
        check.is_forum = bool(getattr(chat, "is_forum", False))

        # 2-3. бот — администратор и право управления темами
        try:
            member = await bot.get_chat_member(chat_id, bot.id)
            status = getattr(member, "status", "")
        except Exception as exc:
            logger.warning("Setup: get_chat_member(%s, bot) недоступен: %s", chat_id, exc)
            status = ""
        check.is_admin = status == "administrator"
        check.can_manage_topics = bool(
            getattr(member, "can_manage_topics", False)
        ) if check.is_admin else False

        # 5-6. группа и настройки в БД
        async with self.session_factory() as session:
            group = await GroupRepository(session).get_by_chat_id(chat_id)
            check.in_db = group is not None
            if group is not None:
                settings = await GroupSettingsRepository(session).get_for(group.id)
                check.settings_ok = settings is not None

        # человекочитаемые проблемы — в порядке шагов инструкции
        if not check.is_admin:
            check.problems.append((
                "❌", "Бот пока не может работать: ему не выданы права администратора.\n\n"
                      "👉 Выдайте Mafia Online права администратора в настройках группы, "
                      "затем повторите /setup."
            ))
        elif not check.can_manage_topics:
            check.problems.append((
                "❌", "Не хватает права «Управление темами».\n\n"
                      "👉 Откройте настройки группы → администраторы → Mafia Online → "
                      "включите «Управление темами», затем повторите /setup."
            ))
        if not check.is_forum:
            check.problems.append((
                "❌", "В этой группе не включены темы форума.\n\n"
                      "👉 Включите «Темы» в настройках группы, затем повторите /setup."
            ))
        return check

    # ------------------------------------------------------------ применение

    async def apply(self, bot, chat_id: int, title: str = "") -> int:
        """Зафиксировать настройку идемпотентно: без дублей записей.

        Возвращает group_id. Группа/настройки создаются только при первом
        обращении; повторный вызов обновляет setup_completed_at и (только
        если форумы партий ещё не заданы) автозаполняет их.
        """
        async with self.session_factory() as session:
            groups = GroupRepository(session)
            group = await groups.get_or_create(chat_id, title or "")
            settings_repo = GroupSettingsRepository(session)
            settings = await settings_repo.get_or_create(group.id)

            # автозаполнение форумов партий: если группа — форум, а форумы
            # не настроены, темы партий создаются прямо в этой группе
            if (
                settings.game_forum_chat_id is None
                and settings.mafia_forum_chat_id is None
            ):
                is_forum = False
                try:
                    chat = await bot.get_chat(chat_id)
                    is_forum = bool(getattr(chat, "is_forum", False))
                except Exception as exc:
                    logger.warning("Setup: get_chat(%s) при применении: %s", chat_id, exc)
                if is_forum:
                    settings.game_forum_chat_id = chat_id
                    settings.mafia_forum_chat_id = chat_id
                    logger.info(
                        "Группа %s: форумы партий автозаполнены самой группой",
                        group.id,
                    )

            settings.setup_completed_at = utcnow()
            await session.commit()
            return group.id


__all__ = ["GroupSetupService", "SetupCheck"]
