"""Add task_assignee table and backfill from legacy single-assignee tasks.

Revision ID: b4d9f01a2c6e
Revises: a81f57c3d9e2
Create Date: 2026-02-13 11:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b4d9f01a2c6e"
down_revision = "a81f57c3d9e2"
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
    if not _has_table("task_assignee"):
        op.create_table(
            "task_assignee",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("assigned_by", sa.Integer(), nullable=True),
            sa.Column("assigned_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["task_id"], ["task.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.ForeignKeyConstraint(["assigned_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", "user_id", name="uq_task_assignee_task_user"),
        )
    else:
        if not _has_unique_constraint("task_assignee", "uq_task_assignee_task_user"):
            op.create_unique_constraint("uq_task_assignee_task_user", "task_assignee", ["task_id", "user_id"])

    if not _has_index("task_assignee", "ix_task_assignee_task_id"):
        op.create_index("ix_task_assignee_task_id", "task_assignee", ["task_id"], unique=False)
    if not _has_index("task_assignee", "ix_task_assignee_user_id"):
        op.create_index("ix_task_assignee_user_id", "task_assignee", ["user_id"], unique=False)
    if not _has_index("task_assignee", "ix_task_assignee_user_task"):
        op.create_index("ix_task_assignee_user_task", "task_assignee", ["user_id", "task_id"], unique=False)

    if _has_table("task") and _has_table("task_assignee"):
        op.execute(
            """
            INSERT INTO task_assignee (task_id, user_id, assigned_by, assigned_at)
            SELECT t.id, t.assigned_to, t.created_by, COALESCE(t.created_at, CURRENT_TIMESTAMP)
            FROM task t
            WHERE t.assigned_to IS NOT NULL
              AND NOT EXISTS (
                SELECT 1
                FROM task_assignee a
                WHERE a.task_id = t.id
                  AND a.user_id = t.assigned_to
              );
            """
        )


def downgrade() -> None:
    if _has_table("task_assignee"):
        op.drop_table("task_assignee")
