"""Add collaborative matter workspace document tables.

Revision ID: 3c6e1f4b8a92
Revises: f8b2c4d7e901
Create Date: 2026-03-11 10:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3c6e1f4b8a92"
down_revision = "f8b2c4d7e901"
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
    if not _has_table("matter_workspace_document"):
        op.create_table(
            "matter_workspace_document",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("matter_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
            sa.Column("template_id", sa.Integer(), nullable=True),
            sa.Column("document_type", sa.String(length=80), nullable=True),
            sa.Column("confidentiality", sa.String(length=80), nullable=True),
            sa.Column("privilege_label", sa.String(length=80), nullable=True),
            sa.Column("retention_category", sa.String(length=80), nullable=True),
            sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("last_edited_by", sa.Integer(), nullable=True),
            sa.Column("published_document_id", sa.Integer(), nullable=True),
            sa.Column("published_version_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_published_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
            sa.ForeignKeyConstraint(["template_id"], ["document_template.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.ForeignKeyConstraint(["last_edited_by"], ["user.id"]),
            sa.ForeignKeyConstraint(["published_document_id"], ["document_record.id"]),
            sa.ForeignKeyConstraint(["published_version_id"], ["document_version.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_matter_id"):
        op.create_index("ix_matter_workspace_document_matter_id", "matter_workspace_document", ["matter_id"], unique=False)
    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_template_id"):
        op.create_index("ix_matter_workspace_document_template_id", "matter_workspace_document", ["template_id"], unique=False)
    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_published_document_id"):
        op.create_index(
            "ix_matter_workspace_document_published_document_id",
            "matter_workspace_document",
            ["published_document_id"],
            unique=False,
        )
    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_published_version_id"):
        op.create_index(
            "ix_matter_workspace_document_published_version_id",
            "matter_workspace_document",
            ["published_version_id"],
            unique=False,
        )
    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_matter_updated"):
        op.create_index(
            "ix_matter_workspace_document_matter_updated",
            "matter_workspace_document",
            ["matter_id", "updated_at"],
            unique=False,
        )
    if not _has_index("matter_workspace_document", "ix_matter_workspace_document_matter_status"):
        op.create_index(
            "ix_matter_workspace_document_matter_status",
            "matter_workspace_document",
            ["matter_id", "status"],
            unique=False,
        )

    if not _has_table("matter_workspace_document_comment"):
        op.create_table(
            "matter_workspace_document_comment",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_document_id", sa.Integer(), nullable=False),
            sa.Column("anchor_label", sa.String(length=120), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_by", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["workspace_document_id"], ["matter_workspace_document.id"]),
            sa.ForeignKeyConstraint(["resolved_by"], ["user.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("matter_workspace_document_comment", "ix_matter_workspace_document_comment_workspace_document_id"):
        op.create_index(
            "ix_matter_workspace_document_comment_workspace_document_id",
            "matter_workspace_document_comment",
            ["workspace_document_id"],
            unique=False,
        )
    if not _has_index("matter_workspace_document_comment", "ix_matter_workspace_document_comment_document_created"):
        op.create_index(
            "ix_matter_workspace_document_comment_document_created",
            "matter_workspace_document_comment",
            ["workspace_document_id", "created_at"],
            unique=False,
        )

    if not _has_table("matter_workspace_document_presence"):
        op.create_table(
            "matter_workspace_document_presence",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_document_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=40), nullable=False, server_default="viewing"),
            sa.Column("cursor_label", sa.String(length=120), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["workspace_document_id"], ["matter_workspace_document.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workspace_document_id",
                "user_id",
                name="uq_matter_workspace_document_presence_user",
            ),
        )
    else:
        if not _has_unique_constraint(
            "matter_workspace_document_presence",
            "uq_matter_workspace_document_presence_user",
        ):
            op.create_unique_constraint(
                "uq_matter_workspace_document_presence_user",
                "matter_workspace_document_presence",
                ["workspace_document_id", "user_id"],
            )

    if not _has_index("matter_workspace_document_presence", "ix_matter_workspace_document_presence_workspace_document_id"):
        op.create_index(
            "ix_matter_workspace_document_presence_workspace_document_id",
            "matter_workspace_document_presence",
            ["workspace_document_id"],
            unique=False,
        )
    if not _has_index("matter_workspace_document_presence", "ix_matter_workspace_document_presence_user_id"):
        op.create_index(
            "ix_matter_workspace_document_presence_user_id",
            "matter_workspace_document_presence",
            ["user_id"],
            unique=False,
        )
    if not _has_index("matter_workspace_document_presence", "ix_matter_workspace_document_presence_document_seen"):
        op.create_index(
            "ix_matter_workspace_document_presence_document_seen",
            "matter_workspace_document_presence",
            ["workspace_document_id", "last_seen_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("matter_workspace_document_presence"):
        op.drop_table("matter_workspace_document_presence")
    if _has_table("matter_workspace_document_comment"):
        op.drop_table("matter_workspace_document_comment")
    if _has_table("matter_workspace_document"):
        op.drop_table("matter_workspace_document")
