"""Backfill trust reconciliation bank statement reference for legacy installs.

Revision ID: e4b7a2f91c0d
Revises: d1a6c4e93f21
Create Date: 2026-02-13 12:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4b7a2f91c0d"
down_revision = "d1a6c4e93f21"
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


def _has_foreign_key(table_name: str, fk_name: str, constrained_column: str | None = None) -> bool:
    if not _has_table(table_name):
        return False
    for fk in _inspector().get_foreign_keys(table_name):
        if fk.get("name") == fk_name:
            return True
        if constrained_column and constrained_column in (fk.get("constrained_columns") or []):
            referred_table = fk.get("referred_table")
            referred_columns = fk.get("referred_columns") or []
            if referred_table == "trust_bank_statement_import" and "id" in referred_columns:
                return True
    return False


def upgrade() -> None:
    if not _has_table("trust_reconciliation_run"):
        return

    if not _has_column("trust_reconciliation_run", "bank_statement_import_id"):
        op.add_column(
            "trust_reconciliation_run",
            sa.Column("bank_statement_import_id", sa.Integer(), nullable=True),
        )

    if (
        _has_column("trust_reconciliation_run", "bank_statement_import_id")
        and _has_table("trust_bank_statement_import")
        and not _has_foreign_key(
            "trust_reconciliation_run",
            "fk_trust_recon_bank_statement_import",
            constrained_column="bank_statement_import_id",
        )
    ):
        op.create_foreign_key(
            "fk_trust_recon_bank_statement_import",
            "trust_reconciliation_run",
            "trust_bank_statement_import",
            ["bank_statement_import_id"],
            ["id"],
        )

    if not _has_index("trust_reconciliation_run", "ix_trust_reconciliation_run_bank_statement_import_id"):
        op.create_index(
            "ix_trust_reconciliation_run_bank_statement_import_id",
            "trust_reconciliation_run",
            ["bank_statement_import_id"],
            unique=False,
        )


def downgrade() -> None:
    if not _has_table("trust_reconciliation_run"):
        return

    if _has_index("trust_reconciliation_run", "ix_trust_reconciliation_run_bank_statement_import_id"):
        op.drop_index(
            "ix_trust_reconciliation_run_bank_statement_import_id",
            table_name="trust_reconciliation_run",
        )
    if _has_foreign_key("trust_reconciliation_run", "fk_trust_recon_bank_statement_import"):
        op.drop_constraint(
            "fk_trust_recon_bank_statement_import",
            "trust_reconciliation_run",
            type_="foreignkey",
        )
    if _has_column("trust_reconciliation_run", "bank_statement_import_id"):
        op.drop_column("trust_reconciliation_run", "bank_statement_import_id")
