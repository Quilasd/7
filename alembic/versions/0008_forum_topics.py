"""Форумные темы партий: game_thread_id и mafia_thread_id в games.

Вместо отдельных Telegram-групп на каждую игру (боты не могут создавать
чаты) используются темы двух ПОСТОЯННЫХ форумов (GAME_FORUM_CHAT_ID /
MAFIA_FORUM_CHAT_ID). game_chat_id/mafia_chat_id теперь хранят ID форумов,
новые поля — message_thread_id тем. Тема создаётся ботом автоматически
(createForumTopic, требуется право администратора can_manage_topics).

Идемпотентна: только две nullable-колонки; старые игры не затрагиваются.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("games")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_thread_id" not in cols:
        op.add_column("games", sa.Column("game_thread_id", sa.Integer(), nullable=True))
    if "mafia_thread_id" not in cols:
        op.add_column("games", sa.Column("mafia_thread_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if "game_thread_id" in cols:
        op.drop_column("games", "game_thread_id")
    if "mafia_thread_id" in cols:
        op.drop_column("games", "mafia_thread_id")
