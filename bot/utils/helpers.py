"""Общие утилиты: время, форматирование, пароли."""

from __future__ import annotations

import hashlib
import html
import secrets
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Наивный UTC (в БД храним именно его, чтобы не путаться в таймзонах)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def esc(value: object | None) -> str:
    """Экранирование пользовательского текста для HTML-parse-mode."""
    return html.escape(str(value if value is not None else ""))


def display_name(user) -> str:
    """Отображаемое имя: display_name -> @username -> 'Игрок N'.

    Решение: у многих игроков нет username, поэтому «красивое имя» хранится
    в таблице users и редактируется в настройках профиля.
    """
    name = (getattr(user, "display_name", "") or "").strip()
    if name:
        return name
    username = (getattr(user, "username", "") or "").strip()
    if username:
        return f"@{username}"
    return f"Игрок {getattr(user, 'telegram_id', 0) % 100000}"


def fmt_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def deadline_in(deadline: datetime | None) -> int:
    """Сколько секунд осталось до дедлайна (0, если он прошёл)."""
    if deadline is None:
        return 0
    return max(0, int((deadline - utcnow()).total_seconds()))


def future(seconds: int) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


# --- Пароли комнат -----------------------------------------------------------
# Для игры достаточно PBKDF2-HMAC-SHA256 из стандартной библиотеки:
# не тянем bcrypt и не храним plaintext.

def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), stored)


def xp_progress_bar(in_level: int, need: int, width: int = 15) -> str:
    """Визуальный прогресс уровня: '██░░░░░░░░░░░░░ 13%'.

    Единый стиль на весь бот (профиль, Owner-панель, level-up).
    Прогресс ограничен 0–100%, деления на ноль нет.
    """
    ratio = 0.0 if need <= 0 else min(1.0, max(0.0, in_level / need))
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {int(round(ratio * 100))}%"


def xp_progress_lines(xp: int, progression=None) -> list[str]:
    """'✨ Опыт: 20 / 150 XP' + бар — по общему XP (единый формат бота)."""
    from bot.services.progression import DEFAULT_PROGRESSION

    progression = progression or DEFAULT_PROGRESSION
    _level, in_level, need = progression.xp_progress_in_level(max(0, xp))
    return [
        f"✨ Опыт: <b>{in_level} / {need}</b> XP",
        xp_progress_bar(in_level, need),
    ]
