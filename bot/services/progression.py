"""ProgressionService: XP и уровни.

Формула уровней задаётся двумя константами и легко меняется:
    threshold(L) = step + (L-2)*growth  — сколько XP нужно на переход L-1 -> L
    L2 = 100, L3 = 250, L4 = 450, L5 = 700 при step=100, growth=50.

Глобальный (User) и локальный (GroupPlayer) прогресс считаются одной
формулой, но хранятся раздельно — сервис работает с числами и не знает,
чьи это XP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressionService:
    step: int = 100    # XP до 2-го уровня
    growth: int = 50   # прибавка к порогу с каждым следующим уровнем

    def threshold(self, level: int) -> int:
        """Суммарный XP, необходимый для ДОСТИЖЕНИЯ уровня (level >= 1)."""
        if level <= 1:
            return 0
        # 100 + 150 + ... ; арифметическая сумма
        n = level - 1
        return n * self.step + self.growth * n * (n - 1) // 2

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
        """(текущий уровень, XP внутри уровня, XP needed до следующего)."""
        level = self.level_for_xp(xp)
        base = self.threshold(level)
        need = self.threshold(level + 1) - base
        return level, max(0, xp) - base, need


DEFAULT_PROGRESSION = ProgressionService()
