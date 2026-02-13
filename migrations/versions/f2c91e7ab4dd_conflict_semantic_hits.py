"""Add conflict semantic evidence table for OCR-backed intake checks.

Revision ID: f2c91e7ab4dd
Revises: e4b7a2f91c0d
Create Date: 2026-02-13 13:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f2c91e7ab4dd"
down_revision = "e4b7a2f91c0d"
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
    if not _has_table("conflict_semantic_hit"):
        op.create_table(
            "conflict_semantic_hit",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conflict_check_id", sa.Integer(), nullable=False),
            sa.Column("document_ocr_text_id", sa.Integer(), nullable=False),
            sa.Column("document_version_id", sa.Integer(), nullable=True),
            sa.Column("matter_id", sa.Integer(), nullable=True),
            sa.Column("candidate_entity", sa.String(length=255), nullable=False),
            sa.Column("matched_phrase", sa.String(length=255), nullable=True),
            sa.Column("match_reason", sa.String(length=255), nullable=True),
            sa.Column("similarity_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("lexical_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("vector_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("excerpt", sa.Text(), nullable=True),
            sa.Column("semantic_rank", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["conflict_check_id"], ["conflict_check.id"]),
            sa.ForeignKeyConstraint(["document_ocr_text_id"], ["document_ocr_text.id"]),
            sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"]),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
        )

    index_specs = [
        ("ix_conflict_semantic_hit_conflict_check_id", ["conflict_check_id"]),
        ("ix_conflict_semantic_hit_document_ocr_text_id", ["document_ocr_text_id"]),
        ("ix_conflict_semantic_hit_document_version_id", ["document_version_id"]),
        ("ix_conflict_semantic_hit_matter_id", ["matter_id"]),
        ("ix_conflict_semantic_hit_conflict_rank", ["conflict_check_id", "semantic_rank"]),
        ("ix_conflict_semantic_hit_similarity", ["similarity_score", "created_at"]),
    ]
    for index_name, columns in index_specs:
        if not _has_index("conflict_semantic_hit", index_name):
            op.create_index(index_name, "conflict_semantic_hit", columns, unique=False)


def downgrade() -> None:
    if not _has_table("conflict_semantic_hit"):
        return

    for index_name in [
        "ix_conflict_semantic_hit_similarity",
        "ix_conflict_semantic_hit_conflict_rank",
        "ix_conflict_semantic_hit_matter_id",
        "ix_conflict_semantic_hit_document_version_id",
        "ix_conflict_semantic_hit_document_ocr_text_id",
        "ix_conflict_semantic_hit_conflict_check_id",
    ]:
        if _has_index("conflict_semantic_hit", index_name):
            op.drop_index(index_name, table_name="conflict_semantic_hit")

    op.drop_table("conflict_semantic_hit")
