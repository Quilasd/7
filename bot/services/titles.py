"""Система титулов (реестр + связь с достижениями).

Титулы открываются:
- через достижения (TITLE_UNLOCKS — автоматически при получении достижения);
- администрацией (команда /title_grant, source='admin');
- за ивенты (через выдачу ивентовых наград, source='event').

Игрок выбирает ОДИН активный титул (users.active_title). Реестр определений
живёт в коде; факты открытия — в БД (user_titles).

Расширение: добавь Title в _ALL_TITLES и (опционально) запись в TITLE_UNLOCKS.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Title:
    id: str
    name: str
    emoji: str = "🎖️"


_ALL_TITLES: list[Title] = [
    Title("sleuth", "Шерлок", "🎖️"),        # за detective_accurate
    Title("guardian", "Хранитель", "🛡️"),    # за shield
    Title("healer", "Целитель", "❤️"),       # за savior
    Title("predator", "Хищник", "🔪"),       # за hunter
    Title("lonewolf", "Последний герой", "👑"),  # за last_survivor
    Title("veteran", "Ветеран", "🔵"),       # за city_win
    Title("mastermind", "Теневой карт", "🧠"),   # за mafia_win
    Title("unstoppable", "Неудержимый", "🔥"),   # за unstoppable (5 побед)
    Title("legend", "Легенда", "🏆"),         # за legendary_streak (10 побед)
    Title("rookie", "Новобранец", "🎯"),      # за first_win
]

_REGISTRY: dict[str, Title] = {t.id: t.id and t for t in _ALL_TITLES}

# Какие достижения открывают какой титул (achievement_id -> title_id)
TITLE_UNLOCKS: dict[str, str] = {
    "first_win": "rookie",
    "savior": "healer",
    "shield": "guardian",
    "detective_accurate": "sleuth",
    "hunter": "predator",
    "last_survivor": "lonewolf",
    "city_win": "veteran",
    "mafia_win": "mastermind",
    "unstoppable": "unstoppable",
    "legendary_streak": "legend",
}


def get_title(title_id: str | None) -> Title | None:
    if not title_id:
        return None
    return _REGISTRY.get(title_id)


def title_display(title_id: str | None) -> str:
    """Готовая строка для профиля вида «🎖️ Шерлок»."""
    title = get_title(title_id)
    if title is None:
        return ""
    return f"{title.emoji} {title.name}"


__all__ = ["Title", "get_title", "title_display", "TITLE_UNLOCKS"]
