from __future__ import annotations

import sqlalchemy as sa

from .extensions import db


# Additive compatibility sync for environments without migration tooling.
# Keeps existing data and only adds missing columns/tables.
COLUMN_PATCHES: dict[str, list[str]] = {
    "user": [
        "mfa_enabled BOOLEAN NOT NULL DEFAULT 0",
        "mfa_secret VARCHAR(64)",
        "failed_login_attempts INTEGER NOT NULL DEFAULT 0",
        "locked_until DATETIME",
        "last_failed_login_at DATETIME",
        "password_changed_at DATETIME",
    ],
    "matter": [
        "objective TEXT",
        "risk_level VARCHAR(40) NOT NULL DEFAULT 'Medium'",
        "budget_status VARCHAR(60) NOT NULL DEFAULT 'On Track'",
        "outcome_summary TEXT",
        "last_update_note TEXT",
        "last_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "court_name VARCHAR(255)",
        "judge_name VARCHAR(255)",
        "jurisdiction VARCHAR(80)",
        "stage VARCHAR(80)",
        "practice_area VARCHAR(120)",
        "case_type VARCHAR(120)",
        "risk_taxonomy VARCHAR(120)",
        "archival_status VARCHAR(40) DEFAULT 'active'",
        "archival_due_at DATETIME",
        "closing_checklist_json TEXT",
        "originating_partner_id INTEGER",
        "supervising_partner_id INTEGER",
    ],
    "document_file": [
        "category VARCHAR(80)",
        "doc_version VARCHAR(40)",
        "lifecycle_stage VARCHAR(40) NOT NULL DEFAULT 'Draft'",
        "owner_name VARCHAR(255)",
        "is_privileged BOOLEAN NOT NULL DEFAULT 0",
    ],
    "task": [
        "priority VARCHAR(20) NOT NULL DEFAULT 'Medium'",
        "sla_hours INTEGER",
        "approval_state VARCHAR(20) NOT NULL DEFAULT 'draft'",
        "requires_two_person_review BOOLEAN NOT NULL DEFAULT 0",
        "recurrence_rule VARCHAR(120)",
        "approved_by INTEGER",
        "approved_at DATETIME",
        "locked_at DATETIME",
    ],
    "trust_reconciliation_run": [
        "bank_statement_import_id INTEGER",
    ],
    "payment_allocation": [
        "status VARCHAR(20) NOT NULL DEFAULT 'settled'",
        "settled_at DATETIME",
        "settled_by INTEGER",
        "external_txn_id VARCHAR(120)",
        "processor_note TEXT",
    ],
}


def _table_names() -> set[str]:
    return set(sa.inspect(db.engine).get_table_names())


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(db.engine)
    return {c["name"] for c in inspector.get_columns(table_name)}


def _apply_sqlite_trust_guards(bind, table_names: set[str]) -> None:
    if "trust_ledger_entry" not in table_names or "trust_client_ledger" not in table_names:
        return
    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_ledger_amount_positive
        BEFORE INSERT ON trust_ledger_entry
        FOR EACH ROW
        WHEN NEW.amount <= 0
        BEGIN
            SELECT RAISE(ABORT, 'trust ledger amount must be positive');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_ledger_type_valid
        BEFORE INSERT ON trust_ledger_entry
        FOR EACH ROW
        WHEN NEW.entry_type NOT IN ('deposit', 'disbursement', 'transfer', 'reversal')
        BEGIN
            SELECT RAISE(ABORT, 'invalid trust ledger entry_type');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_ledger_account_match
        BEFORE INSERT ON trust_ledger_entry
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1
            FROM trust_client_ledger l
            WHERE l.id = NEW.client_ledger_id
              AND l.trust_account_id = NEW.trust_account_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'client ledger does not belong to trust account');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_ledger_no_update
        BEFORE UPDATE ON trust_ledger_entry
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'trust_ledger_entry is immutable');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_ledger_no_delete
        BEFORE DELETE ON trust_ledger_entry
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'trust_ledger_entry is immutable');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_client_ledger_nonnegative_insert
        BEFORE INSERT ON trust_client_ledger
        FOR EACH ROW
        WHEN NEW.current_balance < 0
        BEGIN
            SELECT RAISE(ABORT, 'trust client balance cannot be negative');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_trust_client_ledger_nonnegative_update
        BEFORE UPDATE ON trust_client_ledger
        FOR EACH ROW
        WHEN NEW.current_balance < 0
        BEGIN
            SELECT RAISE(ABORT, 'trust client balance cannot be negative');
        END;
        """,
    ]
    with bind.begin() as conn:
        for sql in statements:
            conn.execute(sa.text(sql))


def _apply_sqlite_legal_hold_guards(bind, table_names: set[str]) -> None:
    if "matter" not in table_names or "legal_hold" not in table_names:
        return
    statements = [
        """
        CREATE TRIGGER IF NOT EXISTS trg_matter_no_delete_on_legal_hold
        BEFORE DELETE ON matter
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM legal_hold lh
            WHERE lh.matter_id = OLD.id
              AND lh.is_active = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'legal hold prevents matter deletion');
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_matter_archive_block_on_legal_hold
        BEFORE UPDATE OF archival_status ON matter
        FOR EACH ROW
        WHEN NEW.archival_status IN ('archive_pending', 'archived', 'deleted')
             AND EXISTS (
                SELECT 1 FROM legal_hold lh
                WHERE lh.matter_id = NEW.id
                  AND lh.is_active = 1
             )
        BEGIN
            SELECT RAISE(ABORT, 'legal hold prevents matter archival');
        END;
        """,
    ]
    if "document_record" in table_names:
        statements.append(
            """
            CREATE TRIGGER IF NOT EXISTS trg_document_record_no_delete_on_legal_hold
            BEFORE DELETE ON document_record
            FOR EACH ROW
            WHEN OLD.legal_hold = 1 OR EXISTS (
                SELECT 1 FROM legal_hold lh
                WHERE lh.matter_id = OLD.matter_id
                  AND lh.is_active = 1
            )
            BEGIN
                SELECT RAISE(ABORT, 'legal hold prevents document deletion');
            END;
            """
        )
    if "document_version" in table_names and "document_record" in table_names:
        statements.append(
            """
            CREATE TRIGGER IF NOT EXISTS trg_document_version_no_delete_on_legal_hold
            BEFORE DELETE ON document_version
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1
                FROM document_record dr
                LEFT JOIN legal_hold lh ON lh.matter_id = dr.matter_id AND lh.is_active = 1
                WHERE dr.id = OLD.document_id
                  AND (lh.id IS NOT NULL OR dr.legal_hold = 1)
            )
            BEGIN
                SELECT RAISE(ABORT, 'legal hold prevents document version deletion');
            END;
            """
        )
    if "document_file" in table_names:
        statements.append(
            """
            CREATE TRIGGER IF NOT EXISTS trg_document_file_no_delete_on_legal_hold
            BEFORE DELETE ON document_file
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1 FROM legal_hold lh
                WHERE lh.matter_id = OLD.matter_id
                  AND lh.is_active = 1
            )
            BEGIN
                SELECT RAISE(ABORT, 'legal hold prevents document file deletion');
            END;
            """
        )
    with bind.begin() as conn:
        for sql in statements:
            conn.execute(sa.text(sql))


def _apply_postgres_trust_guards(bind, table_names: set[str]) -> None:
    if "trust_ledger_entry" not in table_names or "trust_client_ledger" not in table_names:
        return
    statements = [
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
        """,
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
        """,
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
        """,
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
        """,
        """
        CREATE OR REPLACE FUNCTION prevent_trust_ledger_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'trust_ledger_entry is immutable';
        END;
        $$ LANGUAGE plpgsql;
        """,
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
        """,
        "DROP TRIGGER IF EXISTS trg_trust_ledger_insert_guard ON trust_ledger_entry;",
        """
        CREATE TRIGGER trg_trust_ledger_insert_guard
        BEFORE INSERT ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION enforce_trust_ledger_insert_guard();
        """,
        "DROP TRIGGER IF EXISTS trg_trust_ledger_no_update ON trust_ledger_entry;",
        """
        CREATE TRIGGER trg_trust_ledger_no_update
        BEFORE UPDATE ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION prevent_trust_ledger_mutation();
        """,
        "DROP TRIGGER IF EXISTS trg_trust_ledger_no_delete ON trust_ledger_entry;",
        """
        CREATE TRIGGER trg_trust_ledger_no_delete
        BEFORE DELETE ON trust_ledger_entry
        FOR EACH ROW
        EXECUTE FUNCTION prevent_trust_ledger_mutation();
        """,
        "DROP TRIGGER IF EXISTS trg_trust_client_ledger_nonnegative ON trust_client_ledger;",
        """
        CREATE TRIGGER trg_trust_client_ledger_nonnegative
        BEFORE INSERT OR UPDATE ON trust_client_ledger
        FOR EACH ROW
        EXECUTE FUNCTION enforce_trust_client_balance_nonnegative();
        """,
    ]
    with bind.begin() as conn:
        for sql in statements:
            conn.execute(sa.text(sql))


def _apply_postgres_legal_hold_guards(bind, table_names: set[str]) -> None:
    if "matter" not in table_names or "legal_hold" not in table_names:
        return
    statements = [
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
        """,
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
        """,
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
        """,
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
        """,
        "DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_update ON matter;",
        "DROP TRIGGER IF EXISTS trg_matter_legal_hold_guard_delete ON matter;",
        """
        CREATE TRIGGER trg_matter_legal_hold_guard_update
        BEFORE UPDATE OF archival_status ON matter
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_hold_on_matter();
        """,
        """
        CREATE TRIGGER trg_matter_legal_hold_guard_delete
        BEFORE DELETE ON matter
        FOR EACH ROW
        EXECUTE FUNCTION enforce_legal_hold_on_matter();
        """,
    ]
    if "document_record" in table_names:
        statements.extend(
            [
                "DROP TRIGGER IF EXISTS trg_document_record_legal_hold_guard_delete ON document_record;",
                """
                CREATE TRIGGER trg_document_record_legal_hold_guard_delete
                BEFORE DELETE ON document_record
                FOR EACH ROW
                EXECUTE FUNCTION enforce_legal_hold_on_document_record();
                """,
            ]
        )
    if "document_version" in table_names:
        statements.extend(
            [
                "DROP TRIGGER IF EXISTS trg_document_version_legal_hold_guard_delete ON document_version;",
                """
                CREATE TRIGGER trg_document_version_legal_hold_guard_delete
                BEFORE DELETE ON document_version
                FOR EACH ROW
                EXECUTE FUNCTION enforce_legal_hold_on_document_version();
                """,
            ]
        )
    if "document_file" in table_names:
        statements.extend(
            [
                "DROP TRIGGER IF EXISTS trg_document_file_legal_hold_guard_delete ON document_file;",
                """
                CREATE TRIGGER trg_document_file_legal_hold_guard_delete
                BEFORE DELETE ON document_file
                FOR EACH ROW
                EXECUTE FUNCTION enforce_legal_hold_on_document_file();
                """,
            ]
        )
    with bind.begin() as conn:
        for sql in statements:
            conn.execute(sa.text(sql))


def _apply_postgres_rls_policies(bind, table_names: set[str]) -> None:
    if "matter" not in table_names:
        return

    statements = [
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
        """,
        """
        CREATE OR REPLACE FUNCTION app_user_role()
        RETURNS text AS $$
        BEGIN
            RETURN NULLIF(current_setting('app.user_role', true), '');
        END;
        $$ LANGUAGE plpgsql STABLE;
        """,
        """
        CREATE OR REPLACE FUNCTION app_is_admin()
        RETURNS boolean AS $$
        BEGIN
            RETURN COALESCE(NULLIF(current_setting('app.is_admin', true), '')::boolean, FALSE);
        EXCEPTION WHEN others THEN
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """,
        """
        CREATE OR REPLACE FUNCTION app_service_account()
        RETURNS boolean AS $$
        BEGIN
            RETURN COALESCE(NULLIF(current_setting('app.service_account', true), '')::boolean, FALSE);
        EXCEPTION WHEN others THEN
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """,
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
        """,
    ]
    with bind.begin() as conn:
        for sql in statements:
            conn.execute(sa.text(sql))

    table_policy_map = {
        "matter": ("rls_matter_access", "app_matter_visible(id)"),
        "task": ("rls_task_access", "app_matter_visible(matter_id)"),
        "time_entry": ("rls_time_entry_access", "app_matter_visible(matter_id)"),
        "invoice": ("rls_invoice_access", "app_matter_visible(matter_id)"),
        "document_file": ("rls_document_file_access", "app_matter_visible(matter_id)"),
        "document_record": ("rls_document_record_access", "app_matter_visible(matter_id)"),
    }
    with bind.begin() as conn:
        for table_name, (policy_name, predicate) in table_policy_map.items():
            if table_name not in table_names:
                continue
            conn.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;"))
            conn.execute(sa.text(f"DROP POLICY IF EXISTS {policy_name} ON {table_name};"))
            conn.execute(
                sa.text(
                    f"""
                    CREATE POLICY {policy_name}
                    ON {table_name}
                    FOR ALL
                    USING ({predicate})
                    WITH CHECK ({predicate});
                    """
                )
            )

        if "invoice_line" in table_names and "invoice" in table_names:
            conn.execute(sa.text("ALTER TABLE invoice_line ENABLE ROW LEVEL SECURITY;"))
            conn.execute(sa.text("DROP POLICY IF EXISTS rls_invoice_line_access ON invoice_line;"))
            conn.execute(
                sa.text(
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
            )

        if "document_version" in table_names and "document_record" in table_names:
            conn.execute(sa.text("ALTER TABLE document_version ENABLE ROW LEVEL SECURITY;"))
            conn.execute(sa.text("DROP POLICY IF EXISTS rls_document_version_access ON document_version;"))
            conn.execute(
                sa.text(
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
            )

        trust_predicate = "(app_service_account() OR app_is_admin() OR app_user_role() = 'lawyer')"
        for trust_table in ["trust_account", "trust_client_ledger", "trust_ledger_entry", "trust_reconciliation_run", "trust_threshold_alert"]:
            if trust_table not in table_names:
                continue
            policy_name = f"rls_{trust_table}_access"
            conn.execute(sa.text(f"ALTER TABLE {trust_table} ENABLE ROW LEVEL SECURITY;"))
            conn.execute(sa.text(f"DROP POLICY IF EXISTS {policy_name} ON {trust_table};"))
            conn.execute(
                sa.text(
                    f"""
                    CREATE POLICY {policy_name}
                    ON {trust_table}
                    FOR ALL
                    USING {trust_predicate}
                    WITH CHECK {trust_predicate};
                    """
                )
            )


def _apply_runtime_guards(bind, table_names: set[str]) -> None:
    if bind.dialect.name == "sqlite":
        _apply_sqlite_trust_guards(bind, table_names)
        _apply_sqlite_legal_hold_guards(bind, table_names)
    elif bind.dialect.name == "postgresql":
        _apply_postgres_trust_guards(bind, table_names)
        _apply_postgres_legal_hold_guards(bind, table_names)
        _apply_postgres_rls_policies(bind, table_names)


def sync_schema_compatibility() -> None:
    # Create any new tables defined in SQLAlchemy metadata.
    db.create_all()

    bind = db.session.get_bind()
    table_names = _table_names()

    for table_name, patches in COLUMN_PATCHES.items():
        if table_name not in table_names:
            continue
        existing = _column_names(table_name)
        for patch in patches:
            col_name = patch.split(" ", 1)[0]
            if col_name in existing:
                continue
            with bind.begin() as conn:
                conn.execute(sa.text(f"ALTER TABLE {table_name} ADD COLUMN {patch}"))

    _apply_runtime_guards(bind, table_names)
    db.session.commit()
