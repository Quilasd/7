"""Социальные функции, достижения, титулы, ивенты + серии побед.

Добавляет:
- users.win_streak, users.best_win_streak, users.active_title, users.active_event_reward_id
- group_players.win_streak, group_players.best_win_streak
- новые таблицы: death_notes, friend_requests, friendships, user_blocks,
  favorite_players, user_achievements, user_titles, event_rewards,
  user_event_rewards

Идемпотентна: пропускает уже существующие таблицы/колонки (БД могла быть
создана через create_all из актуальных моделей). Существующие данные
не уничтожаются.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    # --- новые колонки в users ------------------------------------------------
    user_columns = _columns(bind, "users")
    if "win_streak" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("win_streak", sa.Integer(), nullable=False, server_default="0"))
    if "best_win_streak" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("best_win_streak", sa.Integer(), nullable=False, server_default="0"))
    if "active_title" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("active_title", sa.String(48), nullable=True))
    if "active_event_reward_id" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("active_event_reward_id", sa.Integer(), nullable=True))

    # --- новые колонки в group_players ---------------------------------------
    gp_columns = _columns(bind, "group_players")
    if "win_streak" not in gp_columns:
        with op.batch_alter_table("group_players") as batch:
            batch.add_column(sa.Column("win_streak", sa.Integer(), nullable=False, server_default="0"))
    if "best_win_streak" not in gp_columns:
        with op.batch_alter_table("group_players") as batch:
            batch.add_column(sa.Column("best_win_streak", sa.Integer(), nullable=False, server_default="0"))

    # --- предсмертные записки -------------------------------------------------
    if "death_notes" not in tables:
        op.create_table(
            "death_notes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("game_id", sa.Integer(), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("text", sa.String(300), nullable=True),
            sa.Column("death_day", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("game_id", "user_id", name="uq_death_note"),
        )
        op.create_index("ix_death_notes_game_id", "death_notes", ["game_id"])
        op.create_index("ix_death_notes_user_id", "death_notes", ["user_id"])
        op.create_index("ix_death_notes_pending", "death_notes", ["game_id", "published"])

    # --- соцсеть: друзья ------------------------------------------------------
    if "friend_requests" not in tables:
        op.create_table(
            "friend_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("from_user_id", "to_user_id", name="uq_friend_request"),
        )
        op.create_index("ix_friend_requests_from_user_id", "friend_requests", ["from_user_id"])
        op.create_index("ix_friend_requests_to", "friend_requests", ["to_user_id"])

    if "friendships" not in tables:
        op.create_table(
            "friendships",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("friend_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "friend_id", name="uq_friendship"),
        )
        op.create_index("ix_friendships_user", "friendships", ["user_id"])
        op.create_index("ix_friendships_friend_id", "friendships", ["friend_id"])

    # --- игнор-лист -----------------------------------------------------------
    if "user_blocks" not in tables:
        op.create_table(
            "user_blocks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("blocked_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "blocked_id", name="uq_user_block"),
        )
        op.create_index("ix_user_blocks_user", "user_blocks", ["user_id"])
        op.create_index("ix_user_blocks_blocked_id", "user_blocks", ["blocked_id"])

    # --- избранное ------------------------------------------------------------
    if "favorite_players" not in tables:
        op.create_table(
            "favorite_players",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("favorite_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "favorite_id", name="uq_favorite"),
        )
        op.create_index("ix_favorites_user", "favorite_players", ["user_id"])
        op.create_index("ix_favorites_favorite_id", "favorite_players", ["favorite_id"])

    # --- достижения -----------------------------------------------------------
    if "user_achievements" not in tables:
        op.create_table(
            "user_achievements",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("achievement_id", sa.String(48), nullable=False),
            sa.Column("awarded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),
        )
        op.create_index("ix_user_achievements_user", "user_achievements", ["user_id"])

    # --- титулы ---------------------------------------------------------------
    if "user_titles" not in tables:
        op.create_table(
            "user_titles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title_id", sa.String(48), nullable=False),
            sa.Column("source", sa.String(24), nullable=False, server_default="achievement"),
            sa.Column("awarded_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "title_id", name="uq_user_title"),
        )
        op.create_index("ix_user_titles_user", "user_titles", ["user_id"])

    # --- ивентовые награды/роли ----------------------------------------------
    if "event_rewards" not in tables:
        op.create_table(
            "event_rewards",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(48), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("emoji", sa.String(16), nullable=False, server_default="🎪"),
            sa.Column("description", sa.String(256), nullable=False, server_default=""),
            sa.Column("kind", sa.String(16), nullable=False, server_default="badge"),
            sa.Column("expires_days", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("code", name="uq_event_reward_code"),
        )

    if "user_event_rewards" not in tables:
        op.create_table(
            "user_event_rewards",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reward_id", sa.Integer(), sa.ForeignKey("event_rewards.id", ondelete="CASCADE"), nullable=False),
            sa.Column("awarded_by", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("awarded_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_user_event_rewards_user", "user_event_rewards", ["user_id"])
        op.create_index("ix_user_event_rewards_reward_id", "user_event_rewards", ["reward_id"])


def _index_names(bind, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    def _drop_idx(name: str, table: str) -> None:
        if name in _index_names(bind, table):
            op.drop_index(name, table_name=table)

    for t in ("user_event_rewards", "event_rewards", "user_titles",
              "user_achievements", "favorite_players", "user_blocks",
              "friendships", "friend_requests", "death_notes"):
        if t in tables:
            op.drop_table(t)

    for col in ("active_event_reward_id", "active_title", "best_win_streak", "win_streak"):
        if col in _columns(bind, "users"):
            with op.batch_alter_table("users") as batch:
                batch.drop_column(col)
    for col in ("best_win_streak", "win_streak"):
        if col in _columns(bind, "group_players"):
            with op.batch_alter_table("group_players") as batch:
                batch.drop_column(col)
