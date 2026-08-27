"""ProgressionService: XP и уровни.

Прогрессия УСЛОЖНЁННАЯ: требование XP на каждый следующий уровень растёт.
Требования на переход (с уровня L на L+1):
    L1 = 150, L2 = 350, L3 = 530,
    дальше прирост требования +20 XP за уровень:
    L4 = 730, L5 = 950, L6 = 1190, L7 = 1450, ...
    req(L) = 530 + 200*(L-3) + 10*(L-3)*(L-4)  при L >= 4.

Суммарный XP для ДОСТИЖЕНИЯ уровня L — threshold(L):
    L1=0, L2=150, L3=500, L4=1030, L5=1760, L6=2710, ...

Глобальный (User) и локальный (GroupPlayer) прогресс считаются одной
формулой, но хранятся раздельно — сервис работает с числами и не знает,
чьи это XP.
"""

from __future__ import annotations

from dataclasses import dataclass

_LEVEL_TABLE: dict[int, int] = {1: 150, 2: 350, 3: 530}


@dataclass(frozen=True)
class ProgressionService:
    """Единая кривая уровней: и для игрока, и для групп, и для Owner-инструментов."""

    def requirement(self, level: int) -> int:
        """Сколько XP нужно набрать ВНУТРИ уровня, чтобы перейти на следующий."""
        if level < 1:
            return 0
        if level in _LEVEL_TABLE:
            return _LEVEL_TABLE[level]
        # L >= 4: 530 + 200*(L-3) + 10*(L-3)*(L-4)
        n = level - 3
        return 530 + 200 * n + 10 * n * (n - 1)

    def threshold(self, level: int) -> int:
        """Суммарный XP, необходимый для ДОСТИЖЕНИЯ уровня (level >= 1)."""
        if level <= 1:
            return 0
        return sum(self.requirement(l) for l in range(1, level))

    def level_for_xp(self, xp: int) -> int:
        level = 1
        while self.threshold(level + 1) <= max(0, xp):
            level += 1
        return level

    def xp_for_next_level(self, xp: int) -> int:
        """Сколько XP осталось до следующего уровня."""
        level = self.level_for_xp(xp)
        return max(0, self.threshold(level + 1) - max(0, xp))

    def xp_progress_in_level(self, xp: int) -> tuple[int, int, int]:
        """(текущий уровень, XP внутри уровня, сколько нужно до следующего)."""
        level = self.level_for_xp(xp)
        base = self.threshold(level)
        need = self.requirement(level)
        return level, max(0, xp) - base, need


DEFAULT_PROGRESSION = ProgressionService()
