"""Add CRM lead quotation workflow table.

Revision ID: ab8d4f2c1e90
Revises: f2c91e7ab4dd
Create Date: 2026-02-13 16:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ab8d4f2c1e90"
down_revision = "f2c91e7ab4dd"
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
    if not _has_table("lead_quote"):
        op.create_table(
            "lead_quote",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("lead_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("fee_model", sa.String(length=40), nullable=False, server_default="fixed"),
            sa.Column("currency", sa.String(length=8), nullable=False, server_default="ZAR"),
            sa.Column("estimated_amount", sa.Float(), nullable=False, server_default="0"),
            sa.Column("estimated_hours", sa.Float(), nullable=True),
            sa.Column("hourly_rate", sa.Float(), nullable=True),
            sa.Column("disbursement_estimate", sa.Float(), nullable=False, server_default="0"),
            sa.Column("tax_rate", sa.Float(), nullable=False, server_default="15"),
            sa.Column("scope_summary", sa.Text(), nullable=True),
            sa.Column("assumptions", sa.Text(), nullable=True),
            sa.Column("valid_until", sa.Date(), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
            sa.Column("status_note", sa.Text(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decided_by", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["lead_id"], ["crm_lead.id"]),
            sa.ForeignKeyConstraint(["decided_by"], ["user.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("lead_quote", "ix_lead_quote_lead_id"):
        op.create_index("ix_lead_quote_lead_id", "lead_quote", ["lead_id"], unique=False)
    if not _has_index("lead_quote", "ix_lead_quote_lead_created"):
        op.create_index("ix_lead_quote_lead_created", "lead_quote", ["lead_id", "created_at"], unique=False)
    if not _has_index("lead_quote", "ix_lead_quote_status_valid"):
        op.create_index("ix_lead_quote_status_valid", "lead_quote", ["status", "valid_until"], unique=False)


def downgrade() -> None:
    if _has_table("lead_quote"):
        op.drop_table("lead_quote")
