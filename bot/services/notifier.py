"""Слой уведомлений.

Движок игры не знает про Telegram: он общается с Notifier-протоколом.
TelegramNotifier оборачивает aiogram Bot, в тестах используется FakeNotifier.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(
        self, user_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> bool: ...


class TelegramNotifier:
    """Отправка ЛС с обработкой ошибок Telegram.

    Если пользователь заблокировал бота (Forbidden), помечаем это в БД,
    чтобы не пытаться слать ему сообщения дальше.
    """

    def __init__(self, bot: Bot, session_factory: async_sessionmaker) -> None:
        self.bot = bot
        self.session_factory = session_factory

    async def send(
        self, user_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> bool:
        if user_id <= 0:
            # Тестовые боты (DEBUG MODE) имеют отрицательные telegram_id —
            # в Telegram им ничего не отправляем.
            logger.debug("Пропуск отправки тестовому боту user_id=%s", user_id)
            return False
        try:
            await self.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return True
        except TelegramForbiddenError:
            logger.warning("Пользователь %s заблокировал бота", user_id)
            await self._mark_blocked(user_id)
            return False
        except TelegramAPIError as exc:
            logger.error("Не удалось отправить сообщение %s: %s", user_id, exc)
            return False
        except Exception as exc:  # pragma: no cover - страховка
            logger.exception("Неожиданная ошибка отправки %s: %s", user_id, exc)
            return False

    async def _mark_blocked(self, telegram_id: int) -> None:
        from bot.database.repositories.users import UserRepository

        try:
            async with self.session_factory() as session:
                repo = UserRepository(session)
                user = await repo.get_by_telegram_id(telegram_id)
                if user and user.can_receive_dm:
                    user.can_receive_dm = False
                    await session.commit()
        except Exception:  # pragma: no cover
            logger.exception("Не удалось пометить can_receive_dm=False")

    async def broadcast(
        self, telegram_ids: list[int], text: str, delay: float = 0.05
    ) -> tuple[int, int]:
        """Рассылка с учётом лимитов Telegram. Возвращает (успех, провал)."""
        ok = failed = 0
        for tg_id in telegram_ids:
            sent = await self.send(tg_id, text)
            ok += int(sent)
            failed += int(not sent)
            await asyncio.sleep(delay)
        return ok, failed


class FakeNotifier:
    """Заглушка для тестов: собирает все отправленные сообщения."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send(
        self, user_id: int, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> bool:
        self.sent.append((user_id, text, keyboard))
        return True

    def messages_to(self, telegram_id: int) -> list[str]:
        return [text for uid, text, _ in self.sent if uid == telegram_id]

    def contains(self, telegram_id: int, fragment: str) -> bool:
        return any(fragment in text for text in self.messages_to(telegram_id))
