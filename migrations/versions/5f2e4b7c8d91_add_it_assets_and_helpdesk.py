"""Add IT asset inventory and helpdesk tables.

Revision ID: 5f2e4b7c8d91
Revises: 3c6e1f4b8a92
Create Date: 2026-03-14 11:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5f2e4b7c8d91"
down_revision = "3c6e1f4b8a92"
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


def upgrade() -> None:
    if not _has_table("it_asset"):
        op.create_table(
            "it_asset",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("asset_tag", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=180), nullable=False),
            sa.Column("asset_type", sa.String(length=60), nullable=False, server_default="laptop"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="in_stock"),
            sa.Column("serial_number", sa.String(length=120), nullable=True),
            sa.Column("vendor", sa.String(length=180), nullable=True),
            sa.Column("location", sa.String(length=180), nullable=True),
            sa.Column("assigned_user_id", sa.Integer(), nullable=True),
            sa.Column("purchase_date", sa.Date(), nullable=True),
            sa.Column("warranty_expires_on", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["assigned_user_id"], ["user.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("asset_tag"),
        )

    if not _has_index("it_asset", "ix_it_asset_asset_tag"):
        op.create_index("ix_it_asset_asset_tag", "it_asset", ["asset_tag"], unique=True)
    if not _has_index("it_asset", "ix_it_asset_assigned_user_id"):
        op.create_index("ix_it_asset_assigned_user_id", "it_asset", ["assigned_user_id"], unique=False)
    if not _has_index("it_asset", "ix_it_asset_status_updated"):
        op.create_index("ix_it_asset_status_updated", "it_asset", ["status", "updated_at"], unique=False)

    if not _has_table("helpdesk_ticket"):
        op.create_table(
            "helpdesk_ticket",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticket_no", sa.String(length=40), nullable=False),
            sa.Column("subject", sa.String(length=180), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(length=60), nullable=False, server_default="general"),
            sa.Column("priority", sa.String(length=20), nullable=False, server_default="medium"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="new"),
            sa.Column("reporter_user_id", sa.Integer(), nullable=False),
            sa.Column("assigned_to", sa.Integer(), nullable=True),
            sa.Column("asset_id", sa.Integer(), nullable=True),
            sa.Column("first_response_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolution_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["reporter_user_id"], ["user.id"]),
            sa.ForeignKeyConstraint(["assigned_to"], ["user.id"]),
            sa.ForeignKeyConstraint(["asset_id"], ["it_asset.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ticket_no"),
        )

    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_ticket_no"):
        op.create_index("ix_helpdesk_ticket_ticket_no", "helpdesk_ticket", ["ticket_no"], unique=True)
    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_reporter_user_id"):
        op.create_index("ix_helpdesk_ticket_reporter_user_id", "helpdesk_ticket", ["reporter_user_id"], unique=False)
    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_assigned_to"):
        op.create_index("ix_helpdesk_ticket_assigned_to", "helpdesk_ticket", ["assigned_to"], unique=False)
    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_asset_id"):
        op.create_index("ix_helpdesk_ticket_asset_id", "helpdesk_ticket", ["asset_id"], unique=False)
    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_status_updated"):
        op.create_index("ix_helpdesk_ticket_status_updated", "helpdesk_ticket", ["status", "updated_at"], unique=False)
    if not _has_index("helpdesk_ticket", "ix_helpdesk_ticket_reporter_status"):
        op.create_index(
            "ix_helpdesk_ticket_reporter_status",
            "helpdesk_ticket",
            ["reporter_user_id", "status"],
            unique=False,
        )

    if not _has_table("helpdesk_ticket_comment"):
        op.create_table(
            "helpdesk_ticket_comment",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticket_id", sa.Integer(), nullable=False),
            sa.Column("author_user_id", sa.Integer(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["ticket_id"], ["helpdesk_ticket.id"]),
            sa.ForeignKeyConstraint(["author_user_id"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("helpdesk_ticket_comment", "ix_helpdesk_ticket_comment_ticket_id"):
        op.create_index("ix_helpdesk_ticket_comment_ticket_id", "helpdesk_ticket_comment", ["ticket_id"], unique=False)
    if not _has_index("helpdesk_ticket_comment", "ix_helpdesk_ticket_comment_author_user_id"):
        op.create_index(
            "ix_helpdesk_ticket_comment_author_user_id",
            "helpdesk_ticket_comment",
            ["author_user_id"],
            unique=False,
        )
    if not _has_index("helpdesk_ticket_comment", "ix_helpdesk_ticket_comment_ticket_created"):
        op.create_index(
            "ix_helpdesk_ticket_comment_ticket_created",
            "helpdesk_ticket_comment",
            ["ticket_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("helpdesk_ticket_comment"):
        op.drop_table("helpdesk_ticket_comment")
    if _has_table("helpdesk_ticket"):
        op.drop_table("helpdesk_ticket")
    if _has_table("it_asset"):
        op.drop_table("it_asset")
