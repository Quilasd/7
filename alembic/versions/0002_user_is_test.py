"""users.is_test: флаг тестовых ботов (DEBUG MODE)

Идемпотентна: если БД создана через create_all из актуальных моделей
(колонка уже есть) — миграция ничего не делает.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind) -> bool:
    inspector = sa.inspect(bind)
    return "is_test" in {c["name"] for c in inspector.get_columns("users")}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        return  # уже есть (например, схема создана create_all из моделей)
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_users_is_test", ["is_test"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_is_test")
        batch.drop_column("is_test")
