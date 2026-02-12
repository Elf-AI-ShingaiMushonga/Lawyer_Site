"""Add scaling indexes and uniqueness guards.

Revision ID: a81f57c3d9e2
Revises: f7e9c8b1a0f4
Create Date: 2026-02-12 18:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a81f57c3d9e2"
down_revision = "f7e9c8b1a0f4"
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


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(c["name"] == constraint_name for c in _inspector().get_unique_constraints(table_name))


def _create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    if _has_index(table_name, index_name):
        return
    op.create_index(index_name, table_name, columns, unique=False)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if not _has_table(table_name):
        return
    if not _has_index(table_name, index_name):
        return
    op.drop_index(index_name, table_name=table_name)


def _create_unique_if_missing(constraint_name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    if _has_unique_constraint(table_name, constraint_name):
        return
    op.create_unique_constraint(constraint_name, table_name, columns)


def _drop_unique_if_exists(constraint_name: str, table_name: str) -> None:
    if not _has_table(table_name):
        return
    if not _has_unique_constraint(table_name, constraint_name):
        return
    op.drop_constraint(constraint_name, table_name, type_="unique")


def upgrade() -> None:
    if _has_table("portal_invoice_view"):
        # Keep a single latest row per portal user + invoice before enforcing uniqueness.
        op.execute(
            """
            DELETE FROM portal_invoice_view
            WHERE id NOT IN (
                SELECT keep_id
                FROM (
                    SELECT MAX(id) AS keep_id
                    FROM portal_invoice_view
                    GROUP BY portal_user_id, invoice_id
                ) keep_rows
            );
            """
        )
    _create_unique_if_missing(
        "uq_portal_invoice_view_user_invoice",
        "portal_invoice_view",
        ["portal_user_id", "invoice_id"],
    )

    _create_index_if_missing("ix_matter_member_user_matter", "matter_member", ["user_id", "matter_id"])
    _create_index_if_missing("ix_task_assigned_status_due", "task", ["assigned_to", "status", "due_date"])
    _create_index_if_missing("ix_task_matter_status_due", "task", ["matter_id", "status", "due_date"])
    _create_index_if_missing(
        "ix_ethical_wall_rule_user_state",
        "ethical_wall_rule",
        ["user_id", "is_active", "is_deny", "wall_id"],
    )
    _create_index_if_missing(
        "ix_ethical_wall_matter_matter_wall",
        "ethical_wall_matter",
        ["matter_id", "wall_id"],
    )
    _create_index_if_missing(
        "ix_document_record_matter_type_conf_created",
        "document_record",
        ["matter_id", "document_type", "confidentiality", "created_at"],
    )
    _create_index_if_missing("ix_time_entry_user_start_at", "time_entry", ["user_id", "start_at"])
    _create_index_if_missing("ix_time_entry_matter_start_at", "time_entry", ["matter_id", "start_at"])
    _create_index_if_missing("ix_invoice_matter_created_at", "invoice", ["matter_id", "created_at"])
    _create_index_if_missing(
        "ix_payment_allocation_invoice_allocated",
        "payment_allocation",
        ["invoice_id", "allocated_at"],
    )
    _create_index_if_missing(
        "ix_trust_ledger_entry_reversal_of_entry_id",
        "trust_ledger_entry",
        ["reversal_of_entry_id"],
    )
    _create_index_if_missing(
        "ix_trust_ledger_entry_account_created",
        "trust_ledger_entry",
        ["trust_account_id", "created_at"],
    )
    _create_index_if_missing(
        "ix_portal_matter_access_user_revoked_matter",
        "portal_matter_access",
        ["portal_user_id", "revoked_at", "matter_id"],
    )
    _create_index_if_missing(
        "ix_portal_invoice_view_user_viewed",
        "portal_invoice_view",
        ["portal_user_id", "last_viewed_at"],
    )
    _create_index_if_missing(
        "ix_analytics_metric_snapshot_scope_key",
        "analytics_metric_snapshot",
        ["as_of_date", "scope_type", "scope_id", "metric_key"],
    )
    _create_index_if_missing("ix_job_queue_claim", "job_queue", ["status", "run_after", "lease_until", "created_at"])
    _create_index_if_missing("ix_scheduled_job_active_next_run", "scheduled_job", ["is_active", "next_run_at"])


def downgrade() -> None:
    _drop_index_if_exists("ix_scheduled_job_active_next_run", "scheduled_job")
    _drop_index_if_exists("ix_job_queue_claim", "job_queue")
    _drop_index_if_exists("ix_analytics_metric_snapshot_scope_key", "analytics_metric_snapshot")
    _drop_index_if_exists("ix_portal_invoice_view_user_viewed", "portal_invoice_view")
    _drop_index_if_exists("ix_portal_matter_access_user_revoked_matter", "portal_matter_access")
    _drop_index_if_exists("ix_trust_ledger_entry_account_created", "trust_ledger_entry")
    _drop_index_if_exists("ix_trust_ledger_entry_reversal_of_entry_id", "trust_ledger_entry")
    _drop_index_if_exists("ix_payment_allocation_invoice_allocated", "payment_allocation")
    _drop_index_if_exists("ix_invoice_matter_created_at", "invoice")
    _drop_index_if_exists("ix_time_entry_matter_start_at", "time_entry")
    _drop_index_if_exists("ix_time_entry_user_start_at", "time_entry")
    _drop_index_if_exists("ix_document_record_matter_type_conf_created", "document_record")
    _drop_index_if_exists("ix_ethical_wall_matter_matter_wall", "ethical_wall_matter")
    _drop_index_if_exists("ix_ethical_wall_rule_user_state", "ethical_wall_rule")
    _drop_index_if_exists("ix_task_matter_status_due", "task")
    _drop_index_if_exists("ix_task_assigned_status_due", "task")
    _drop_index_if_exists("ix_matter_member_user_matter", "matter_member")
    _drop_unique_if_exists("uq_portal_invoice_view_user_invoice", "portal_invoice_view")
