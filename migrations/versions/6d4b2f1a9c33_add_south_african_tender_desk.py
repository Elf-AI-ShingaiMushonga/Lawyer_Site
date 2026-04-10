"""Add South African tender desk tables.

Revision ID: 6d4b2f1a9c33
Revises: 5f2e4b7c8d91
Create Date: 2026-03-26 12:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "6d4b2f1a9c33"
down_revision = "5f2e4b7c8d91"
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
    if not _has_table("tender_opportunity"):
        op.create_table(
            "tender_opportunity",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("reference_no", sa.String(length=120), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("issuing_authority", sa.String(length=255), nullable=False),
            sa.Column("province", sa.String(length=80), nullable=False, server_default=sa.text("'National'")),
            sa.Column("sector", sa.String(length=120), nullable=True),
            sa.Column("tender_type", sa.String(length=60), nullable=False, server_default=sa.text("'Tender'")),
            sa.Column("portal_source", sa.String(length=120), nullable=False, server_default=sa.text("'SA eTender Portal'")),
            sa.Column("status", sa.String(length=40), nullable=False, server_default=sa.text("'Sourced'")),
            sa.Column("etender_url", sa.String(length=500), nullable=True),
            sa.Column("briefing_required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("briefing_date", sa.DateTime(), nullable=True),
            sa.Column("closing_at", sa.DateTime(), nullable=False),
            sa.Column("validity_end_date", sa.Date(), nullable=True),
            sa.Column("estimated_value", sa.Float(), nullable=True),
            sa.Column("preference_system", sa.String(length=40), nullable=True),
            sa.Column("cidb_required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("cidb_grading", sa.String(length=40), nullable=True),
            sa.Column("local_content_required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("submission_channel", sa.String(length=80), nullable=True),
            sa.Column("submission_address", sa.Text(), nullable=True),
            sa.Column("contact_person", sa.String(length=255), nullable=True),
            sa.Column("contact_email", sa.String(length=255), nullable=True),
            sa.Column("contact_phone", sa.String(length=80), nullable=True),
            sa.Column("csd_supplier_number", sa.String(length=120), nullable=True),
            sa.Column("tcs_pin", sa.String(length=120), nullable=True),
            sa.Column("bbbee_level", sa.String(length=80), nullable=True),
            sa.Column("bid_manager_user_id", sa.Integer(), nullable=True),
            sa.Column("matter_id", sa.Integer(), nullable=True),
            sa.Column("scope_summary", sa.Text(), nullable=True),
            sa.Column("next_action", sa.Text(), nullable=True),
            sa.Column("internal_notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["bid_manager_user_id"], ["user.id"]),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_index("tender_opportunity", "ix_tender_opportunity_reference_no"):
        op.create_index("ix_tender_opportunity_reference_no", "tender_opportunity", ["reference_no"], unique=False)
    if not _has_index("tender_opportunity", "ix_tender_opportunity_title"):
        op.create_index("ix_tender_opportunity_title", "tender_opportunity", ["title"], unique=False)
    if not _has_index("tender_opportunity", "ix_tender_opportunity_closing_at"):
        op.create_index("ix_tender_opportunity_closing_at", "tender_opportunity", ["closing_at"], unique=False)
    if not _has_index("tender_opportunity", "ix_tender_opportunity_bid_manager_user_id"):
        op.create_index("ix_tender_opportunity_bid_manager_user_id", "tender_opportunity", ["bid_manager_user_id"], unique=False)
    if not _has_index("tender_opportunity", "ix_tender_opportunity_matter_id"):
        op.create_index("ix_tender_opportunity_matter_id", "tender_opportunity", ["matter_id"], unique=False)
    if not _has_index("tender_opportunity", "ix_tender_opportunity_status_closing"):
        op.create_index(
            "ix_tender_opportunity_status_closing",
            "tender_opportunity",
            ["status", "closing_at"],
            unique=False,
        )
    if not _has_index("tender_opportunity", "ix_tender_opportunity_bid_manager_status"):
        op.create_index(
            "ix_tender_opportunity_bid_manager_status",
            "tender_opportunity",
            ["bid_manager_user_id", "status"],
            unique=False,
        )

    if not _has_table("tender_checklist_item"):
        op.create_table(
            "tender_checklist_item",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tender_id", sa.Integer(), nullable=False),
            sa.Column("item_key", sa.String(length=80), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("status", sa.String(length=30), nullable=False, server_default=sa.text("'pending'")),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["tender_id"], ["tender_opportunity.id"]),
            sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tender_id", "item_key", name="uq_tender_checklist_item"),
        )

    if not _has_index("tender_checklist_item", "ix_tender_checklist_item_tender_id"):
        op.create_index("ix_tender_checklist_item_tender_id", "tender_checklist_item", ["tender_id"], unique=False)
    if not _has_index("tender_checklist_item", "ix_tender_checklist_tender_status"):
        op.create_index(
            "ix_tender_checklist_tender_status",
            "tender_checklist_item",
            ["tender_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("tender_checklist_item"):
        op.drop_table("tender_checklist_item")
    if _has_table("tender_opportunity"):
        op.drop_table("tender_opportunity")
