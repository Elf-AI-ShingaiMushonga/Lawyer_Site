"""Backfill payment allocation settlement columns for legacy installs.

Revision ID: d1a6c4e93f21
Revises: b4d9f01a2c6e
Create Date: 2026-02-13 12:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d1a6c4e93f21"
down_revision = "b4d9f01a2c6e"
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
            if referred_table == "user" and "id" in referred_columns:
                return True
    return False


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _has_table("payment_allocation"):
        return

    _add_column_if_missing(
        "payment_allocation",
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'settled'")),
    )
    _add_column_if_missing("payment_allocation", sa.Column("settled_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("payment_allocation", sa.Column("settled_by", sa.Integer(), nullable=True))
    _add_column_if_missing("payment_allocation", sa.Column("external_txn_id", sa.String(length=120), nullable=True))
    _add_column_if_missing("payment_allocation", sa.Column("processor_note", sa.Text(), nullable=True))
    if (
        _has_column("payment_allocation", "settled_by")
        and _has_table("user")
        and not _has_foreign_key(
            "payment_allocation",
            "fk_payment_allocation_settled_by_user",
            constrained_column="settled_by",
        )
    ):
        op.create_foreign_key(
            "fk_payment_allocation_settled_by_user",
            "payment_allocation",
            "user",
            ["settled_by"],
            ["id"],
        )

    if _has_column("payment_allocation", "status"):
        op.execute("UPDATE payment_allocation SET status = 'settled' WHERE status IS NULL;")
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.execute("ALTER TABLE payment_allocation ALTER COLUMN status SET DEFAULT 'settled';")
            op.execute("ALTER TABLE payment_allocation ALTER COLUMN status SET NOT NULL;")

    if not _has_index("payment_allocation", "ix_payment_allocation_invoice_allocated"):
        op.create_index(
            "ix_payment_allocation_invoice_allocated",
            "payment_allocation",
            ["invoice_id", "allocated_at"],
            unique=False,
        )
    if not _has_index("payment_allocation", "ix_payment_allocation_status_settled"):
        op.create_index(
            "ix_payment_allocation_status_settled",
            "payment_allocation",
            ["status", "settled_at"],
            unique=False,
        )


def downgrade() -> None:
    if not _has_table("payment_allocation"):
        return

    if _has_index("payment_allocation", "ix_payment_allocation_status_settled"):
        op.drop_index("ix_payment_allocation_status_settled", table_name="payment_allocation")
    if _has_foreign_key("payment_allocation", "fk_payment_allocation_settled_by_user"):
        op.drop_constraint("fk_payment_allocation_settled_by_user", "payment_allocation", type_="foreignkey")

    if _has_column("payment_allocation", "processor_note"):
        op.drop_column("payment_allocation", "processor_note")
    if _has_column("payment_allocation", "external_txn_id"):
        op.drop_column("payment_allocation", "external_txn_id")
    if _has_column("payment_allocation", "settled_by"):
        op.drop_column("payment_allocation", "settled_by")
    if _has_column("payment_allocation", "settled_at"):
        op.drop_column("payment_allocation", "settled_at")
    if _has_column("payment_allocation", "status"):
        op.drop_column("payment_allocation", "status")
