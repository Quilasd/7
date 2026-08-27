"""Игровые чаты партии: game_chat_id и mafia_chat_id в games.

Telegram Bot API НЕ позволяет ботам создавать чаты и добавлять участников,
поэтому чаты создаёт игрок (создатель партии), добавляет бота админом и
привязывает командами /gamechat и /mafiachat. Бот управляет правами,
анонсами и модерацией. Старые игры — NULL (функция опциональна).

Идемпотентна и безопасна: только добавляет две nullable-колонки.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("games")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_chat_id" not in cols:
        op.add_column("games", sa.Column("game_chat_id", sa.BigInteger(), nullable=True))
    if "mafia_chat_id" not in cols:
        op.add_column("games", sa.Column("mafia_chat_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_chat_id" in cols:
        op.drop_column("games", "game_chat_id")
    if "mafia_chat_id" in cols:
        op.drop_column("games", "mafia_chat_id")
