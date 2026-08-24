"""initial schema: users, rooms, games, actions, votes, app_settings

Revision ID: 0001
Revises:
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Исходная схема создаётся из единого источника правды — моделей
    # SQLAlchemy (bot.database.models). Такой приём гарантирует, что миграция
    # не разойдётся с моделями, и отлично работает для первой миграции.
    from bot.database.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from bot.database.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
