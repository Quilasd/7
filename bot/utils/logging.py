"""Настройка логирования: консоль + ротатирующийся файл.

Важно: в логи сознательно НЕ пишутся секретные роли и персональные
ночные действия — только идентификаторы игр/пользователей и счётчики.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Инициализация root-логгера. Вызывается один раз при старте."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Приглушаем шумные библиотеки
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def read_log_tail(log_file: str, lines: int = 40) -> str:
    """Последние строки лог-файла для админ-панели."""
    path = Path(log_file)
    if not path.exists():
        return "Лог-файл ещё не создан."
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:]) or "Лог пуст."
    except OSError as exc:  # pragma: no cover - защита от гонок с ротацией
        return f"Не удалось прочитать лог: {exc}"
