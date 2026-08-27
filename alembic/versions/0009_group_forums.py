"""Пер-group игровые форумы (ТЗ-11): game_forum_chat_id/mafia_forum_chat_id в group_settings.

Глобальные GAME_FORUM_CHAT_ID/MAFIA_FORUM_CHAT_ID больше НЕ являются источником
форумов для игр групп: каждая группа настраивает СВОИ форумы локально
(/set_game_forum в самой группе). Глобальные env-значения остаются только
fallback-ом для игр БЕЗ группы (комнаты, созданные в личном чате с ботом).

Идемпотентна: только две nullable-колонки.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("group_settings")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_forum_chat_id" not in cols:
        op.add_column(
            "group_settings",
            sa.Column("game_forum_chat_id", sa.BigInteger(), nullable=True),
        )
    if "mafia_forum_chat_id" not in cols:
        op.add_column(
            "group_settings",
            sa.Column("mafia_forum_chat_id", sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_forum_chat_id" in cols:
        op.drop_column("group_settings", "game_forum_chat_id")
    if "mafia_forum_chat_id" in cols:
        op.drop_column("group_settings", "mafia_forum_chat_id")
