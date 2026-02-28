"""Add legal category and archetype fields to matters and matter templates.

Revision ID: b9d4a8c6e2f1
Revises: e91b7c3d4a10
Create Date: 2026-02-28 10:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b9d4a8c6e2f1"
down_revision = "e91b7c3d4a10"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_foreign_key(table_name: str, fk_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any((fk.get("name") or "") == fk_name for fk in _inspector().get_foreign_keys(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if _has_table("matter_template"):
        if not _has_column("matter_template", "legal_category"):
            op.add_column("matter_template", sa.Column("legal_category", sa.String(length=120), nullable=True))
        if not _has_column("matter_template", "required_fields_json"):
            op.add_column("matter_template", sa.Column("required_fields_json", sa.Text(), nullable=True))
        if not _has_column("matter_template", "boilerplate_template"):
            op.add_column("matter_template", sa.Column("boilerplate_template", sa.Text(), nullable=True))

    if _has_table("matter"):
        if not _has_column("matter", "legal_category"):
            op.add_column("matter", sa.Column("legal_category", sa.String(length=120), nullable=True))
        if not _has_column("matter", "archetype_id"):
            op.add_column("matter", sa.Column("archetype_id", sa.Integer(), nullable=True))
        if not _has_column("matter", "archetype_data_json"):
            op.add_column("matter", sa.Column("archetype_data_json", sa.Text(), nullable=True))

        if not _has_index("matter", "ix_matter_legal_category"):
            op.create_index("ix_matter_legal_category", "matter", ["legal_category"], unique=False)
        if not _has_index("matter", "ix_matter_archetype_id"):
            op.create_index("ix_matter_archetype_id", "matter", ["archetype_id"], unique=False)

        if dialect != "sqlite" and not _has_foreign_key("matter", "fk_matter_archetype_id_matter_template"):
            op.create_foreign_key(
                "fk_matter_archetype_id_matter_template",
                "matter",
                "matter_template",
                ["archetype_id"],
                ["id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if _has_table("matter"):
        if dialect != "sqlite" and _has_foreign_key("matter", "fk_matter_archetype_id_matter_template"):
            op.drop_constraint("fk_matter_archetype_id_matter_template", "matter", type_="foreignkey")
        if _has_index("matter", "ix_matter_archetype_id"):
            op.drop_index("ix_matter_archetype_id", table_name="matter")
        if _has_index("matter", "ix_matter_legal_category"):
            op.drop_index("ix_matter_legal_category", table_name="matter")
        if _has_column("matter", "archetype_data_json"):
            op.drop_column("matter", "archetype_data_json")
        if _has_column("matter", "archetype_id"):
            op.drop_column("matter", "archetype_id")
        if _has_column("matter", "legal_category"):
            op.drop_column("matter", "legal_category")

    if _has_table("matter_template"):
        if _has_column("matter_template", "boilerplate_template"):
            op.drop_column("matter_template", "boilerplate_template")
        if _has_column("matter_template", "required_fields_json"):
            op.drop_column("matter_template", "required_fields_json")
        if _has_column("matter_template", "legal_category"):
            op.drop_column("matter_template", "legal_category")
