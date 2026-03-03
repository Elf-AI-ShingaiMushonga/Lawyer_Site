"""Add AI operation logging and semantic index tables (phase 0/1).

Revision ID: c7a8e9f0a1b2
Revises: b9d4a8c6e2f1
Create Date: 2026-03-03 10:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7a8e9f0a1b2"
down_revision = "b9d4a8c6e2f1"
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
    if not _has_table("ai_operation_log"):
        op.create_table(
            "ai_operation_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("operation_type", sa.String(length=80), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False, server_default="openai"),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
            sa.Column("request_chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("response_units", sa.Integer(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("redaction_applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    for index_name, columns in [
        ("ix_ai_operation_log_operation_type", ["operation_type"]),
        ("ix_ai_operation_log_status", ["status"]),
        ("ix_ai_operation_log_created_at", ["created_at"]),
    ]:
        if not _has_index("ai_operation_log", index_name):
            op.create_index(index_name, "ai_operation_log", columns, unique=False)

    if not _has_table("semantic_index_entry"):
        op.create_table(
            "semantic_index_entry",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_type", sa.String(length=40), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=False),
            sa.Column("matter_id", sa.Integer(), nullable=True),
            sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("content_text", sa.Text(), nullable=False),
            sa.Column("content_sha256", sa.String(length=64), nullable=False),
            sa.Column("embedding_json", sa.Text(), nullable=False),
            sa.Column("embedding_model", sa.String(length=120), nullable=True),
            sa.Column("embedding_dim", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("provider", sa.String(length=40), nullable=True),
            sa.Column("redaction_meta_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["matter_id"], ["matter.id"]),
            sa.UniqueConstraint("source_type", "source_id", "chunk_index", name="uq_semantic_index_source_chunk"),
        )

    for index_name, columns in [
        ("ix_semantic_index_entry_source_type", ["source_type"]),
        ("ix_semantic_index_entry_source_id", ["source_id"]),
        ("ix_semantic_index_entry_matter_id", ["matter_id"]),
        ("ix_semantic_index_entry_content_sha256", ["content_sha256"]),
        ("ix_semantic_index_matter_source", ["matter_id", "source_type", "source_id"]),
        ("ix_semantic_index_source_updated", ["source_type", "source_id", "updated_at"]),
    ]:
        if not _has_index("semantic_index_entry", index_name):
            op.create_index(index_name, "semantic_index_entry", columns, unique=False)


def downgrade() -> None:
    if _has_table("semantic_index_entry"):
        for index_name in [
            "ix_semantic_index_source_updated",
            "ix_semantic_index_matter_source",
            "ix_semantic_index_entry_content_sha256",
            "ix_semantic_index_entry_matter_id",
            "ix_semantic_index_entry_source_id",
            "ix_semantic_index_entry_source_type",
        ]:
            if _has_index("semantic_index_entry", index_name):
                op.drop_index(index_name, table_name="semantic_index_entry")
        op.drop_table("semantic_index_entry")

    if _has_table("ai_operation_log"):
        for index_name in [
            "ix_ai_operation_log_created_at",
            "ix_ai_operation_log_status",
            "ix_ai_operation_log_operation_type",
        ]:
            if _has_index("ai_operation_log", index_name):
                op.drop_index(index_name, table_name="ai_operation_log")
        op.drop_table("ai_operation_log")
