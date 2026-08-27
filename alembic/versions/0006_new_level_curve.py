"""Новая кривая уровней: пересчёт сохранённых level по XP.

Прогрессия уровней усложнена (150/350/530 и далее рост). Поле level —
производное от xp, поэтому после смены формулы пересчитываем его у всех
существующих строк (users и group_players), иначе рейтинги уровней были бы
считаны по старой формуле до следующей игры.

Идемпотентна: пересчёт можно выполнять сколько угодно раз (level = f(xp)).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _level_for_xp(xp: int) -> int:
    """Копия новой формулы ProgressionService (миграция не импортирует bot.*)."""
    table = {1: 150, 2: 350, 3: 530}

    def requirement(level: int) -> int:
        if level < 1:
            return 0
        if level in table:
            return table[level]
        n = level - 3
        return 530 + 200 * n + 10 * n * (n - 1)

    def threshold(level: int) -> int:
        if level <= 1:
            return 0
        return sum(requirement(l) for l in range(1, level))

    level = 1
    while threshold(level + 1) <= max(0, xp):
        level += 1
    return level


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, pk in (("users", "id"), ("group_players", "id")):
        if table not in tables:
            continue
        rows = bind.execute(sa.text(f"SELECT {pk}, xp, level FROM {table}"))
        for row_id, xp, _old_level in rows:
            new_level = _level_for_xp(int(xp or 0))
            bind.execute(
                sa.text(f"UPDATE {table} SET level = :lvl WHERE {pk} = :pid"),
                {"lvl": new_level, "pid": row_id},
            )


def downgrade() -> None:
    """Возврат к старой формуле (100 + 50 на уровень): пересчёт обратно."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    def old_level(xp: int) -> int:
        level, need, remaining = 1, 100, max(0, xp)
        while remaining >= need:
            remaining -= need
            level += 1
            need += 50
        return level

    for table, pk in (("users", "id"), ("group_players", "id")):
        if table not in tables:
            continue
        rows = bind.execute(sa.text(f"SELECT {pk}, xp FROM {table}"))
        for row_id, xp in rows:
            bind.execute(
                sa.text(f"UPDATE {table} SET level = :lvl WHERE {pk} = :pid"),
                {"lvl": old_level(int(xp or 0)), "pid": row_id},
            )
