"""Add contract template table linked to optional matter archetypes.

Revision ID: f8b2c4d7e901
Revises: c7a8e9f0a1b2
Create Date: 2026-03-03 16:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f8b2c4d7e901"
down_revision = "c7a8e9f0a1b2"
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
    if not _has_table("contract_template"):
        op.create_table(
            "contract_template",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("legal_category", sa.String(length=120), nullable=True),
            sa.Column("archetype_id", sa.Integer(), nullable=True),
            sa.Column("contract_type", sa.String(length=80), nullable=False, server_default="Contract"),
            sa.Column("required_fields_json", sa.Text(), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("requires_signature", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("auto_create_on_matter_open", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_by", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["archetype_id"], ["matter_template.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["user.id"]),
            sa.UniqueConstraint("name", name="uq_contract_template_name"),
        )

    if not _has_index("contract_template", "ix_contract_template_archetype_active"):
        op.create_index(
            "ix_contract_template_archetype_active",
            "contract_template",
            ["archetype_id", "is_active"],
            unique=False,
        )

    if not _has_index("contract_template", "ix_contract_template_archetype_id"):
        op.create_index("ix_contract_template_archetype_id", "contract_template", ["archetype_id"], unique=False)


def downgrade() -> None:
    if _has_table("contract_template"):
        for index_name in [
            "ix_contract_template_archetype_id",
            "ix_contract_template_archetype_active",
        ]:
            if _has_index("contract_template", index_name):
                op.drop_index(index_name, table_name="contract_template")
        op.drop_table("contract_template")
