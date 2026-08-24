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


# --- Уровни и опыт -----------------------------------------------------------

def level_from_xp(xp: int) -> int:
    """Уровень растёт квадратично: на каждый следующий нужно больше XP.

    Переход 1->2: 100 XP, 2->3: 150 XP, 3->4: 200 XP и т.д.
    """
    level = 1
    need = 100
    remaining = max(0, xp)
    while remaining >= need:
        remaining -= need
        level += 1
        need += 50
    return level


def xp_for_next_level(xp: int) -> int:
    """Сколько XP осталось до следующего уровня (для профиля)."""
    level = 1
    need = 100
    remaining = max(0, xp)
    while remaining >= need:
        remaining -= need
        level += 1
        need += 50
    return need - remaining
