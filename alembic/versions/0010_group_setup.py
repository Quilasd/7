"""Первичная настройка сервера (/setup): group_settings.setup_completed_at.

Отмечает время последнего успешного /setup (бот-админ + can_manage_topics +
форумные темы). NULL = группа ещё не настроена; существующие группы и данные
не затрагиваются. Идемпотентна: одна nullable-колонка.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("group_settings")}


def upgrade() -> None:
    bind = op.get_bind()
    if "setup_completed_at" not in _columns(bind):
        op.add_column(
            "group_settings",
            sa.Column("setup_completed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "setup_completed_at" in _columns(bind):
        op.drop_column("group_settings", "setup_completed_at")
