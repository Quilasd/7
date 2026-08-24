"""groups: Group, GroupPlayer, GroupAdmin, GroupSettings, AuditLog
+ users.investigations, users.rating default 0, rooms.group_id, games.group_id

Идемпотентна: пропускает уже существующие таблицы/колонки (БД могла быть
создана через create_all из актуальных моделей). Существующие данные
не уничтожаются.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "groups" not in tables:
        op.create_table(
            "groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
            sa.Column("title", sa.String(128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_groups_telegram_chat_id", "groups", ["telegram_chat_id"], unique=True)
        tables.add("groups")

    if "group_players" not in tables:
        op.create_table(
            "group_players",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("games_played", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("kills", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("saves", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("investigations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("correct_votes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rating", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("xp", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("warnings", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_banned", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("joined_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("group_id", "user_id", name="uq_group_player"),
        )
        op.create_index("ix_group_players_group_id", "group_players", ["group_id"])
        op.create_index("ix_group_players_user_id", "group_players", ["user_id"])
        op.create_index("ix_group_players_top", "group_players", ["group_id", "rating"])

    if "group_admins" not in tables:
        op.create_table(
            "group_admins",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("admin_level", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("group_id", "user_id", name="uq_group_admin"),
        )
        op.create_index("ix_group_admins_group", "group_admins", ["group_id", "admin_level"])
        op.create_index("ix_group_admins_user_id", "group_admins", ["user_id"])

    if "group_settings" not in tables:
        op.create_table(
            "group_settings",
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("min_players", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("max_players", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("night_seconds", sa.Integer(), nullable=False, server_default="90"),
            sa.Column("day_seconds", sa.Integer(), nullable=False, server_default="180"),
            sa.Column("discussion_seconds", sa.Integer(), nullable=False, server_default="180"),
            sa.Column("vote_seconds", sa.Integer(), nullable=False, server_default="60"),
            sa.Column("tie_rule", sa.String(16), nullable=False, server_default="revote"),
            sa.Column("role_reveal_on_death", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("enabled_roles", sa.JSON(), nullable=False),
            sa.Column("mafia_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("allow_maniac", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("xp_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("rating_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("global_xp_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("local_xp_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("global_rating_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("local_rating_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("debug_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if "audit_logs" not in tables:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id"), nullable=True),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("details", sa.String(512), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_audit_group", "audit_logs", ["group_id", "created_at"])
        op.create_index("ix_audit_actor", "audit_logs", ["actor_id", "created_at"])

    user_columns = _columns(bind, "users")
    if "investigations" not in user_columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("investigations", sa.Integer(), nullable=False, server_default="0"))

    room_columns = _columns(bind, "rooms")
    if "group_id" not in room_columns:
        # FK не задаём: batch-пересоздание таблицы в SQLite + безымянный
        # FK ломают повторный batch (Constraint must have a name)
        with op.batch_alter_table("rooms") as batch:
            batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        op.create_index("ix_rooms_group_id", "rooms", ["group_id"])

    game_columns = _columns(bind, "games")
    if "group_id" not in game_columns:
        with op.batch_alter_table("games") as batch:
            batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        op.create_index("ix_games_group_id", "games", ["group_id"])


def _index_names(bind, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def downgrade() -> None:
    bind = op.get_bind()

    def _drop_index_if_exists(name: str, table: str) -> None:
        # индекс нужно снять ДО удаления колонки, иначе batch-пересоздание
        # таблицы в SQLite упадёт на CREATE INDEX по несуществующей колонке
        if name in _index_names(bind, table):
            op.drop_index(name, table_name=table)

    op.drop_table("audit_logs")
    op.drop_table("group_settings")
    op.drop_table("group_admins")
    op.drop_table("group_players")
    _drop_index_if_exists("ix_games_group_id", "games")
    with op.batch_alter_table("games") as batch:
        batch.drop_column("group_id")
    _drop_index_if_exists("ix_rooms_group_id", "rooms")
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("group_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("investigations")
    op.drop_table("groups")
