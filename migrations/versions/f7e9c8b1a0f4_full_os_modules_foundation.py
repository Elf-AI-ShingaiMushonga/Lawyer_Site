"""Full OS modules foundation schema (additive).

Revision ID: f7e9c8b1a0f4
Revises: c2b1f2a8d4e5
Create Date: 2026-02-12 13:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f7e9c8b1a0f4"
down_revision = "c2b1f2a8d4e5"
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


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _create_missing_tables_from_metadata() -> None:
    # Reuse SQLAlchemy metadata to create all newly introduced tables safely.
    from intranet.models import db as model_db

    bind = op.get_bind()
    model_db.metadata.create_all(bind=bind, checkfirst=True)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _apply_postgres_audit_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _has_table("audit_log"):
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_log_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON audit_log;")
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_no_update
        BEFORE UPDATE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_log_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_no_delete
        BEFORE DELETE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_log_mutation();
        """
    )


def _drop_postgres_audit_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _has_table("audit_log"):
        return
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log;")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_log_mutation();")


def _apply_postgres_trust_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_table("trust_ledger_entry") or not _has_table("trust_client_ledger"):
        return

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_trust_client_balance_nonnegative'
            ) THEN
                ALTER TABLE trust_client_ledger
                ADD CONSTRAINT ck_trust_client_balance_nonnegative CHECK (current_balance >= 0);
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_trust_ledger_amount_positive'
            ) THEN
                ALTER TABLE trust_ledger_entry
                ADD CONSTRAINT ck_trust_ledger_amount_positive CHECK (amount > 0);
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_trust_ledger_entry_type'
            ) THEN
                ALTER TABLE trust_ledger_entry
                ADD CONSTRAINT ck_trust_ledger_entry_type
                CHECK (entry_type IN ('deposit', 'disbursement', 'transfer', 'reversal'));
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_trust_ledger_insert_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.amount <= 0 THEN
                RAISE EXCEPTION 'trust ledger amount must be positive';
            END IF;
            IF NEW.entry_type NOT IN ('deposit', 'disbursement', 'transfer', 'reversal') THEN
                RAISE EXCEPTION 'invalid trust ledger entry_type';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM trust_client_ledger l
                WHERE l.id = NEW.client_ledger_id
                  AND l.trust_account_id = NEW.trust_account_id
            ) THEN
                RAISE EXCEPTION 'client ledger does not belong to trust account';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_trust_ledger_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'trust_ledger_entry is immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_trust_client_balance_nonnegative()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.current_balance < 0 THEN
                RAISE EXCEPTION 'trust client balance cannot be negative';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_insert_guard ON trust_ledger_entry;")
    op.execute(
        """
        CREATE TRIGGER trg_trust_ledger_insert_guard
        BEFORE INSERT ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION enforce_trust_ledger_insert_guard();
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_no_update ON trust_ledger_entry;")
    op.execute(
        """
        CREATE TRIGGER trg_trust_ledger_no_update
        BEFORE UPDATE ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION prevent_trust_ledger_mutation();
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_no_delete ON trust_ledger_entry;")
    op.execute(
        """
        CREATE TRIGGER trg_trust_ledger_no_delete
        BEFORE DELETE ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION prevent_trust_ledger_mutation();
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_trust_client_ledger_nonnegative ON trust_client_ledger;")
    op.execute(
        """
        CREATE TRIGGER trg_trust_client_ledger_nonnegative
        BEFORE INSERT OR UPDATE ON trust_client_ledger
        FOR EACH ROW
        EXECUTE FUNCTION enforce_trust_client_balance_nonnegative();
        """
    )


def _drop_postgres_trust_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_table("trust_ledger_entry"):
        op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_insert_guard ON trust_ledger_entry;")
        op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_no_update ON trust_ledger_entry;")
        op.execute("DROP TRIGGER IF EXISTS trg_trust_ledger_no_delete ON trust_ledger_entry;")
    if _has_table("trust_client_ledger"):
        op.execute("DROP TRIGGER IF EXISTS trg_trust_client_ledger_nonnegative ON trust_client_ledger;")
    op.execute("DROP FUNCTION IF EXISTS enforce_trust_ledger_insert_guard();")
    op.execute("DROP FUNCTION IF EXISTS prevent_trust_ledger_mutation();")
    op.execute("DROP FUNCTION IF EXISTS enforce_trust_client_balance_nonnegative();")


def _apply_postgres_legal_hold_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_table("matter") or not _has_table("legal_hold"):
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_hold_on_matter()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF EXISTS (
                    SELECT 1 FROM legal_hold lh
                    WHERE lh.matter_id = OLD.id
                      AND lh.is_active IS TRUE
                ) THEN
                    RAISE EXCEPTION 'legal hold prevents matter deletion';
                END IF;
                RETURN OLD;
            END IF;

            IF NEW.archival_status IN ('archive_pending', 'archived', 'deleted') AND EXISTS (
                SELECT 1 FROM legal_hold lh
                WHERE lh.matter_id = NEW.id
                  AND lh.is_active IS TRUE
            ) THEN
                RAISE EXCEPTION 'legal hold prevents matter archival';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_hold_on_document_record()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM legal_hold lh
                WHERE lh.matter_id = OLD.matter_id
                  AND lh.is_active IS TRUE
            ) OR COALESCE(OLD.legal_hold, FALSE) IS TRUE THEN
                RAISE EXCEPTION 'legal hold prevents document deletion';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_hold_on_document_version()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM document_record dr
                LEFT JOIN legal_hold lh ON lh.matter_id = dr.matter_id AND lh.is_active IS TRUE
                WHERE dr.id = OLD.document_id
                  AND (lh.id IS NOT NULL OR COALESCE(dr.legal_hold, FALSE) IS TRUE)
            ) THEN
                RAISE EXCEPTION 'legal hold prevents document version deletion';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_legal_hold_on_document_file()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM legal_hold lh
                WHERE lh.matter_id = OLD.matter_id
                  AND lh.is_active IS TRUE
            ) THEN
                RAISE EXCEPTION 'legal hold prevents document file deletion';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_update ON matter;")
    op.execute("DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_delete ON matter;")
    op.execute(
        """
        CREATE TRIGGER trg_matter_legal_hold_guard_update
        BEFORE UPDATE OF archival_status ON matter
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_hold_on_matter();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_matter_legal_hold_guard_delete
        BEFORE DELETE ON matter
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_hold_on_matter();
        """
    )

    if _has_table("document_record"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_record_legal_hold_guard_delete ON document_record;")
        op.execute(
            """
            CREATE TRIGGER trg_document_record_legal_hold_guard_delete
            BEFORE DELETE ON document_record
            FOR EACH ROW
            EXECUTE FUNCTION enforce_legal_hold_on_document_record();
            """
        )
    if _has_table("document_version"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_version_legal_hold_guard_delete ON document_version;")
        op.execute(
            """
            CREATE TRIGGER trg_document_version_legal_hold_guard_delete
            BEFORE DELETE ON document_version
            FOR EACH ROW
            EXECUTE FUNCTION enforce_legal_hold_on_document_version();
            """
        )
    if _has_table("document_file"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_file_legal_hold_guard_delete ON document_file;")
        op.execute(
            """
            CREATE TRIGGER trg_document_file_legal_hold_guard_delete
            BEFORE DELETE ON document_file
            FOR EACH ROW
            EXECUTE FUNCTION enforce_legal_hold_on_document_file();
            """
        )


def _drop_postgres_legal_hold_guards() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    if _has_table("matter"):
        op.execute("DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_update ON matter;")
        op.execute("DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_delete ON matter;")
    if _has_table("document_record"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_record_legal_hold_guard_delete ON document_record;")
    if _has_table("document_version"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_version_legal_hold_guard_delete ON document_version;")
    if _has_table("document_file"):
        op.execute("DROP TRIGGER IF EXISTS trg_document_file_legal_hold_guard_delete ON document_file;")

    op.execute("DROP FUNCTION IF EXISTS enforce_legal_hold_on_matter();")
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_hold_on_document_record();")
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_hold_on_document_version();")
    op.execute("DROP FUNCTION IF EXISTS enforce_legal_hold_on_document_file();")


def _apply_postgres_rls_policies() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _has_table("matter"):
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_user_id()
        RETURNS integer AS $$
        DECLARE value_text text;
        BEGIN
            value_text := current_setting('app.current_user_id', true);
            IF value_text IS NULL OR value_text = '' THEN
                RETURN NULL;
            END IF;
            RETURN value_text::integer;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_user_role()
        RETURNS text AS $$
        BEGIN
            RETURN NULLIF(current_setting('app.user_role', true), '');
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_is_admin()
        RETURNS boolean AS $$
        BEGIN
            RETURN COALESCE(NULLIF(current_setting('app.is_admin', true), '')::boolean, FALSE);
        EXCEPTION WHEN others THEN
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_service_account()
        RETURNS boolean AS $$
        BEGIN
            RETURN COALESCE(NULLIF(current_setting('app.service_account', true), '')::boolean, FALSE);
        EXCEPTION WHEN others THEN
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_matter_visible(target_matter_id integer)
        RETURNS boolean AS $$
        DECLARE uid integer;
        BEGIN
            IF app_service_account() OR app_is_admin() THEN
                RETURN TRUE;
            END IF;
            uid := app_current_user_id();
            IF uid IS NULL OR target_matter_id IS NULL THEN
                RETURN FALSE;
            END IF;
            RETURN EXISTS (
                SELECT 1
                FROM matter_member mm
                WHERE mm.matter_id = target_matter_id
                  AND mm.user_id = uid
            ) AND NOT EXISTS (
                SELECT 1
                FROM ethical_wall_matter ewm
                JOIN ethical_wall_rule ewr ON ewr.wall_id = ewm.wall_id
                WHERE ewm.matter_id = target_matter_id
                  AND ewr.user_id = uid
                  AND ewr.is_deny IS TRUE
                  AND ewr.is_active IS TRUE
            );
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )

    table_policies = {
        "matter": (
            "rls_matter_access",
            "app_matter_visible(id)",
        ),
        "task": (
            "rls_task_access",
            "app_matter_visible(matter_id)",
        ),
        "time_entry": (
            "rls_time_entry_access",
            "app_matter_visible(matter_id)",
        ),
        "invoice": (
            "rls_invoice_access",
            "app_matter_visible(matter_id)",
        ),
        "document_file": (
            "rls_document_file_access",
            "app_matter_visible(matter_id)",
        ),
        "document_record": (
            "rls_document_record_access",
            "app_matter_visible(matter_id)",
        ),
    }
    for table_name, (policy_name, predicate) in table_policies.items():
        if not _has_table(table_name):
            continue
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};")
        op.execute(
            f"""
            CREATE POLICY {policy_name}
            ON {table_name}
            FOR ALL
            USING ({predicate})
            WITH CHECK ({predicate});
            """
        )

    if _has_table("invoice_line") and _has_table("invoice"):
        op.execute("ALTER TABLE invoice_line ENABLE ROW LEVEL SECURITY;")
        op.execute("DROP POLICY IF EXISTS rls_invoice_line_access ON invoice_line;")
        op.execute(
            """
            CREATE POLICY rls_invoice_line_access
            ON invoice_line
            FOR ALL
            USING (
                app_service_account()
                OR EXISTS (
                    SELECT 1 FROM invoice inv
                    WHERE inv.id = invoice_line.invoice_id
                      AND app_matter_visible(inv.matter_id)
                )
            )
            WITH CHECK (
                app_service_account()
                OR EXISTS (
                    SELECT 1 FROM invoice inv
                    WHERE inv.id = invoice_line.invoice_id
                      AND app_matter_visible(inv.matter_id)
                )
            );
            """
        )

    if _has_table("document_version") and _has_table("document_record"):
        op.execute("ALTER TABLE document_version ENABLE ROW LEVEL SECURITY;")
        op.execute("DROP POLICY IF EXISTS rls_document_version_access ON document_version;")
        op.execute(
            """
            CREATE POLICY rls_document_version_access
            ON document_version
            FOR ALL
            USING (
                app_service_account()
                OR EXISTS (
                    SELECT 1 FROM document_record dr
                    WHERE dr.id = document_version.document_id
                      AND app_matter_visible(dr.matter_id)
                )
            )
            WITH CHECK (
                app_service_account()
                OR EXISTS (
                    SELECT 1 FROM document_record dr
                    WHERE dr.id = document_version.document_id
                      AND app_matter_visible(dr.matter_id)
                )
            );
            """
        )

    trust_role_predicate = "(app_service_account() OR app_is_admin() OR app_user_role() = 'lawyer')"
    for table_name in ["trust_account", "trust_client_ledger", "trust_ledger_entry", "trust_reconciliation_run", "trust_threshold_alert"]:
        if not _has_table(table_name):
            continue
        policy_name = f"rls_{table_name}_access"
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};")
        op.execute(
            f"""
            CREATE POLICY {policy_name}
            ON {table_name}
            FOR ALL
            USING {trust_role_predicate}
            WITH CHECK {trust_role_predicate};
            """
        )


def _drop_postgres_rls_policies() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    policy_map = {
        "matter": "rls_matter_access",
        "task": "rls_task_access",
        "time_entry": "rls_time_entry_access",
        "invoice": "rls_invoice_access",
        "document_file": "rls_document_file_access",
        "document_record": "rls_document_record_access",
        "document_version": "rls_document_version_access",
        "invoice_line": "rls_invoice_line_access",
        "trust_account": "rls_trust_account_access",
        "trust_client_ledger": "rls_trust_client_ledger_access",
        "trust_ledger_entry": "rls_trust_ledger_entry_access",
        "trust_reconciliation_run": "rls_trust_reconciliation_run_access",
        "trust_threshold_alert": "rls_trust_threshold_alert_access",
    }
    for table_name, policy_name in policy_map.items():
        if not _has_table(table_name):
            continue
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP FUNCTION IF EXISTS app_matter_visible(integer);")
    op.execute("DROP FUNCTION IF EXISTS app_service_account();")
    op.execute("DROP FUNCTION IF EXISTS app_is_admin();")
    op.execute("DROP FUNCTION IF EXISTS app_user_role();")
    op.execute("DROP FUNCTION IF EXISTS app_current_user_id();")


def upgrade() -> None:
    # Extend existing core tables.
    _add_column_if_missing("user", sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    _add_column_if_missing("user", sa.Column("mfa_secret", sa.String(length=64), nullable=True))
    _add_column_if_missing(
        "user", sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default=sa.text("0"))
    )
    _add_column_if_missing("user", sa.Column("locked_until", sa.DateTime(), nullable=True))
    _add_column_if_missing("user", sa.Column("last_failed_login_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("user", sa.Column("password_changed_at", sa.DateTime(), nullable=True))

    _add_column_if_missing("matter", sa.Column("court_name", sa.String(length=255), nullable=True))
    _add_column_if_missing("matter", sa.Column("judge_name", sa.String(length=255), nullable=True))
    _add_column_if_missing("matter", sa.Column("jurisdiction", sa.String(length=80), nullable=True))
    _add_column_if_missing("matter", sa.Column("stage", sa.String(length=80), nullable=True))
    _add_column_if_missing("matter", sa.Column("practice_area", sa.String(length=120), nullable=True))
    _add_column_if_missing("matter", sa.Column("case_type", sa.String(length=120), nullable=True))
    _add_column_if_missing("matter", sa.Column("risk_taxonomy", sa.String(length=120), nullable=True))
    _add_column_if_missing(
        "matter", sa.Column("archival_status", sa.String(length=40), nullable=True, server_default="active")
    )
    _add_column_if_missing("matter", sa.Column("archival_due_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("matter", sa.Column("closing_checklist_json", sa.Text(), nullable=True))
    _add_column_if_missing("matter", sa.Column("originating_partner_id", sa.Integer(), nullable=True))
    _add_column_if_missing("matter", sa.Column("supervising_partner_id", sa.Integer(), nullable=True))

    _add_column_if_missing("task", sa.Column("priority", sa.String(length=20), nullable=False, server_default="Medium"))
    _add_column_if_missing("task", sa.Column("sla_hours", sa.Integer(), nullable=True))
    _add_column_if_missing("task", sa.Column("approval_state", sa.String(length=20), nullable=False, server_default="draft"))
    _add_column_if_missing(
        "task",
        sa.Column("requires_two_person_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    _add_column_if_missing("task", sa.Column("recurrence_rule", sa.String(length=120), nullable=True))
    _add_column_if_missing("task", sa.Column("approved_by", sa.Integer(), nullable=True))
    _add_column_if_missing("task", sa.Column("approved_at", sa.DateTime(), nullable=True))
    _add_column_if_missing("task", sa.Column("locked_at", sa.DateTime(), nullable=True))

    _create_missing_tables_from_metadata()
    _apply_postgres_audit_guards()
    _apply_postgres_trust_guards()
    _apply_postgres_legal_hold_guards()
    _apply_postgres_rls_policies()


def downgrade() -> None:
    _drop_postgres_rls_policies()
    _drop_postgres_legal_hold_guards()
    _drop_postgres_trust_guards()
    _drop_postgres_audit_guards()

    # Drop newly created tables in reverse dependency-ish order.
    new_tables = [
        "dr_target",
        "restore_verification",
        "backup_run",
        "scheduled_job",
        "job_history",
        "job_queue",
        "burnout_signal",
        "workload_forecast",
        "analytics_metric_snapshot",
        "portal_link_token",
        "portal_payment_receipt",
        "portal_invoice_view",
        "portal_upload",
        "portal_message",
        "portal_message_thread",
        "portal_matter_access",
        "portal_user",
        "engagement_letter",
        "conflict_check",
        "intake_form",
        "crm_follow_up",
        "crm_lead",
        "trust_threshold_alert",
        "trust_approval_request",
        "trust_reconciliation_run",
        "trust_ledger_entry",
        "trust_client_ledger",
        "trust_account",
        "expense_entry",
        "payment_allocation",
        "ar_snapshot",
        "ledes_export",
        "invoice_adjustment",
        "invoice_line",
        "invoice",
        "tax_rule",
        "fee_arrangement",
        "rate_card",
        "time_validation_event",
        "time_entry",
        "time_timer",
        "time_rounding_policy",
        "email_capture",
        "bates_range",
        "production_item",
        "production_set",
        "saved_search",
        "document_ocr_text",
        "document_lock",
        "document_version",
        "document_record",
        "task_approval",
        "task_checklist_item",
        "task_dependency",
        "deadline",
        "deadline_rule",
        "holiday_calendar",
        "matter_closing_checklist_item",
        "matter_stage_history",
        "matter_note_acl",
        "matter_note",
        "matter_party",
        "entity_relationship",
        "entity",
        "document_template",
        "task_template_item",
        "task_template",
        "matter_template",
        "timekeeper_role",
        "practice_area",
        "office",
        "firm_setting",
        "notification",
        "suspicious_activity_alert",
        "data_residency_policy",
        "retention_policy",
        "legal_hold",
        "ethical_wall_matter",
        "ethical_wall_rule",
        "ethical_wall",
        "permission_grant",
        "sso_token",
        "sso_authorization_code",
        "sso_application",
        "user_mfa_backup_code",
        "trusted_device",
        "user_session",
    ]
    for table_name in new_tables:
        _drop_table_if_exists(table_name)

    _drop_column_if_exists("task", "locked_at")
    _drop_column_if_exists("task", "approved_at")
    _drop_column_if_exists("task", "approved_by")
    _drop_column_if_exists("task", "recurrence_rule")
    _drop_column_if_exists("task", "requires_two_person_review")
    _drop_column_if_exists("task", "approval_state")
    _drop_column_if_exists("task", "sla_hours")
    _drop_column_if_exists("task", "priority")

    _drop_column_if_exists("matter", "supervising_partner_id")
    _drop_column_if_exists("matter", "originating_partner_id")
    _drop_column_if_exists("matter", "closing_checklist_json")
    _drop_column_if_exists("matter", "archival_due_at")
    _drop_column_if_exists("matter", "archival_status")
    _drop_column_if_exists("matter", "risk_taxonomy")
    _drop_column_if_exists("matter", "case_type")
    _drop_column_if_exists("matter", "practice_area")
    _drop_column_if_exists("matter", "stage")
    _drop_column_if_exists("matter", "jurisdiction")
    _drop_column_if_exists("matter", "judge_name")
    _drop_column_if_exists("matter", "court_name")

    _drop_column_if_exists("user", "password_changed_at")
    _drop_column_if_exists("user", "last_failed_login_at")
    _drop_column_if_exists("user", "locked_until")
    _drop_column_if_exists("user", "failed_login_attempts")
    _drop_column_if_exists("user", "mfa_secret")
    _drop_column_if_exists("user", "mfa_enabled")
