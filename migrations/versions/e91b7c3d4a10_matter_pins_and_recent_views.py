"""Add matter pinning and recent-view tracking.

Revision ID: e91b7c3d4a10
Revises: ab8d4f2c1e90
Create Date: 2026-02-17 10:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e91b7c3d4a10"
down_revision = "ab8d4f2c1e90"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(c["name"] == constraint_name for c in _inspector().get_unique_constraints(table_name))


def upgrade() -> None:
    if not _has_table("matter_pin"):
        op.create_table(
            "matter_pin",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("matter_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "matter_id", name="uq_matter_pin_user_matter"),
        )
    elif not _has_unique_constraint("matter_pin", "uq_matter_pin_user_matter"):
        op.create_unique_constraint("uq_matter_pin_user_matter", "matter_pin", ["user_id", "matter_id"])

    if not _has_index("matter_pin", "ix_matter_pin_user_id"):
        op.create_index("ix_matter_pin_user_id", "matter_pin", ["user_id"], unique=False)
    if not _has_index("matter_pin", "ix_matter_pin_matter_id"):
        op.create_index("ix_matter_pin_matter_id", "matter_pin", ["matter_id"], unique=False)
    if not _has_index("matter_pin", "ix_matter_pin_user_created"):
        op.create_index("ix_matter_pin_user_created", "matter_pin", ["user_id", "created_at"], unique=False)

    if not _has_table("matter_recent_view"):
        op.create_table(
            "matter_recent_view",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("matter_id", sa.Integer(), nullable=False),
            sa.Column("first_viewed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_viewed_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("view_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "matter_id", name="uq_matter_recent_user_matter"),
        )
    elif not _has_unique_constraint("matter_recent_view", "uq_matter_recent_user_matter"):
        op.create_unique_constraint("uq_matter_recent_user_matter", "matter_recent_view", ["user_id", "matter_id"])

    if not _has_index("matter_recent_view", "ix_matter_recent_view_user_id"):
        op.create_index("ix_matter_recent_view_user_id", "matter_recent_view", ["user_id"], unique=False)
    if not _has_index("matter_recent_view", "ix_matter_recent_view_matter_id"):
        op.create_index("ix_matter_recent_view_matter_id", "matter_recent_view", ["matter_id"], unique=False)
    if not _has_index("matter_recent_view", "ix_matter_recent_user_last_viewed"):
        op.create_index(
            "ix_matter_recent_user_last_viewed",
            "matter_recent_view",
            ["user_id", "last_viewed_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("matter_recent_view"):
        op.drop_table("matter_recent_view")
    if _has_table("matter_pin"):
        op.drop_table("matter_pin")
