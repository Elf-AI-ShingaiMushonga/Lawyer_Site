"""Demo story and governance extensions.

Revision ID: c2b1f2a8d4e5
Revises: 9f3a1d4e9c1b
Create Date: 2026-02-10 16:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c2b1f2a8d4e5"
down_revision = "9f3a1d4e9c1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matter", sa.Column("objective", sa.Text(), nullable=True))
    op.add_column("matter", sa.Column("risk_level", sa.String(length=40), nullable=False, server_default="Medium"))
    op.add_column(
        "matter",
        sa.Column("budget_status", sa.String(length=60), nullable=False, server_default="On Track"),
    )
    op.add_column("matter", sa.Column("outcome_summary", sa.Text(), nullable=True))
    op.add_column("matter", sa.Column("last_update_note", sa.Text(), nullable=True))
    op.add_column(
        "matter",
        sa.Column("last_updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.add_column("document_file", sa.Column("category", sa.String(length=80), nullable=True))
    op.add_column("document_file", sa.Column("doc_version", sa.String(length=40), nullable=True))
    op.add_column(
        "document_file",
        sa.Column("lifecycle_stage", sa.String(length=40), nullable=False, server_default="Draft"),
    )
    op.add_column("document_file", sa.Column("owner_name", sa.String(length=255), nullable=True))
    op.add_column(
        "document_file",
        sa.Column("is_privileged", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "matter_timeline_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("matter_id", sa.Integer(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_milestone", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matter_timeline_event_matter_id", "matter_timeline_event", ["matter_id"], unique=False)

    op.create_table(
        "matter_activity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("matter_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matter_activity_matter_id", "matter_activity", ["matter_id"], unique=False)

    op.create_table(
        "governance_incident",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=180), nullable=False),
        sa.Column("incident_type", sa.String(length=60), nullable=False, server_default="Incident"),
        sa.Column("severity", sa.String(length=40), nullable=False, server_default="Medium"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="Open"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("governance_incident")

    op.drop_index("ix_matter_activity_matter_id", table_name="matter_activity")
    op.drop_table("matter_activity")

    op.drop_index("ix_matter_timeline_event_matter_id", table_name="matter_timeline_event")
    op.drop_table("matter_timeline_event")

    op.drop_column("document_file", "is_privileged")
    op.drop_column("document_file", "owner_name")
    op.drop_column("document_file", "lifecycle_stage")
    op.drop_column("document_file", "doc_version")
    op.drop_column("document_file", "category")

    op.drop_column("matter", "last_updated_at")
    op.drop_column("matter", "last_update_note")
    op.drop_column("matter", "outcome_summary")
    op.drop_column("matter", "budget_status")
    op.drop_column("matter", "risk_level")
    op.drop_column("matter", "objective")
