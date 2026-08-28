"""Таймеры фаз на asyncio-задачах (не блокируют event loop).

TimerManager хранит по одной активной задаче на игру; повторное расписание
для той же игры отменяет предыдущее. После рестарта бота состояние игр
восстанавливается из БД (PhaseManager.recover) и таймеры перепланируются
по дедлайнам, записанным в games.phase_deadline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class TimerManager:
    def __init__(self) -> None:
        self._tasks: dict[tuple[int, str], asyncio.Task] = {}

    def schedule(
        self,
        game_id: int,
        phase: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self.cancel(game_id, phase)
        self._tasks[(game_id, phase)] = asyncio.create_task(
            self._run(game_id, phase, delay_seconds, callback),
            name=f"timer-{game_id}-{phase}",
        )

    async def _run(self, game_id: int, phase: str, delay: float, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await callback()
        except asyncio.CancelledError:
            logger.debug("Таймер %s-%s отменён", game_id, phase)
        except Exception:
            logger.exception("Сбой таймера %s-%s", game_id, phase)

    def cancel(self, game_id: int, phase: str | None = None) -> None:
        """Отмена таймеров игры.

        ВАЖНО: не отменяем ТЕКУЩУЮ задачу. Переходы фаз выполняются внутри
        таймерных задач, и `_end_game`/`_schedule` вызывают cancel() из них же;
        self-cancel убил бы выполнение на первом же await (CancelledError),
        и финальная обработка игры (статистика/XP/сообщения) молча умерла бы.
        Текущая задача исключается из реестра и завершится сама.
        """
        current = asyncio.current_task()
        keys = [k for k in self._tasks if k[0] == game_id and (phase is None or k[1] == phase)]
        for key in keys:
            task = self._tasks.pop(key, None)
            if task and not task.done() and task is not current:
                task.cancel()

    def cancel_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    def active_count(self) -> int:
        return len(self._tasks)


class NoopTimerManager(TimerManager):
    """Для тестов: ничего не планирует (переходы вызываются вручную)."""

    def schedule(self, game_id, phase, delay_seconds, callback) -> None:  # noqa: D102
        logger.debug("NoopTimer: пропущен таймер %s-%s", game_id, phase)
