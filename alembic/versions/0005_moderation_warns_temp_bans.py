"""Модерация 2.0: варны с причиной/сроком, временные баны, новое распределение прав.

Добавляет:
- таблицу group_warnings (варн: причина, срок действия, отзыв);
- group_players.banned_until (временный локальный бан; NULL = навсегда);
- group_settings.warn_limit / warn_expire_hours / warn_ban_minutes.

Идемпотентна: пропускает уже существующие таблицы/колонки.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "group_warnings" not in tables:
        op.create_table(
            "group_warnings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index(
            "ix_group_warnings_group_user", "group_warnings", ["group_id", "user_id"]
        )
        # имя как у индекса из модели (user_id index=True), чтобы схема
        # совпадала и на БД, где таблицу создал create_all из 0001
        op.create_index("ix_group_warnings_user_id", "group_warnings", ["user_id"])

    gp_cols = _columns(bind, "group_players")
    if "banned_until" not in gp_cols:
        op.add_column("group_players", sa.Column("banned_until", sa.DateTime(), nullable=True))

    gs_cols = _columns(bind, "group_settings")
    defaults = [
        ("warn_limit", "3"),
        ("warn_expire_hours", "168"),
        ("warn_ban_minutes", "1440"),
    ]
    for name, default in defaults:
        if name not in gs_cols:
            op.add_column(
                "group_settings",
                sa.Column(name, sa.Integer(), nullable=False, server_default=default),
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    gs_cols = _columns(bind, "group_settings")
    for name in ("warn_limit", "warn_expire_hours", "warn_ban_minutes"):
        if name in gs_cols:
            op.drop_column("group_settings", name)
    if "banned_until" in _columns(bind, "group_players"):
        op.drop_column("group_players", "banned_until")
    if "group_warnings" in tables:
        # имена индексов зависят от того, кто создал таблицу (миграция или
        # create_all из 0001) — снимаем фактические по интроспекции
        for idx in sa.inspect(bind).get_indexes("group_warnings"):
            op.drop_index(idx["name"], table_name="group_warnings")
        op.drop_table("group_warnings")
