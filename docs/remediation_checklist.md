# Law Firm OS Remediation Checklist

Date: 2026-02-12

This checklist converts the functionality audit into an execution backlog with strict priority tiers.

Status values:
- `done` = implemented and validated in code/tests.
- `in_progress` = partially implemented, additional work required.
- `pending` = not implemented.

## Priority 0 - Must Fix (Security/Compliance/Financial Integrity)

| ID | Checklist item | Status | Evidence |
|---|---|---|---|
| P0-A1 | Conflict report export workflow | done | `intranet/routes/crm.py` (`/crm/conflicts/<id>/export`), `intranet/reports/conflicts.py` |
| P0-A2 | Invoice write-down/write-off with reason and audit | done | `intranet/routes/billing.py` (`/billing/invoices/<id>/adjust`), `InvoiceAdjustment` |
| P0-A3 | AR aging report + collection notes | done | `intranet/routes/billing.py` (`/billing/ar-aging`), `ARSnapshot` |
| P0-A4 | Portal time-limited links for specific documents | done | `intranet/routes/portal.py` (`/portal/links`, `/portal/link/<token>`), `PortalLinkToken` |
| P0-A5 | DMS saved-search workflow | done | `intranet/routes/dms.py` (`/dms/saved-searches`), `SavedSearch` |
| P0-A6 | Email capture workflow with dedup and attachment hash | done | `intranet/routes/dms.py` (`/matters/<id>/email-capture`), `EmailCapture` |
| P0-A7 | Deadline escalation job for unacknowledged critical deadlines | done | `intranet/jobs/worker.py` (`deadline_escalation_scan`) |
| P0-A8 | Deadline digest notifications by user/team scope | done | `intranet/jobs/worker.py` (`deadline_digest`), `intranet/jobs/scheduler.py` |
| P0-A9 | Row-level security strategy for core tables | done | `intranet/db_context.py`, `intranet/schema_sync.py` (`_apply_postgres_rls_policies`), `migrations/versions/f7e9c8b1a0f4_full_os_modules_foundation.py` |
| P0-A10 | Legal hold enforcement in destructive flows | done | `intranet/routes/matters_plus.py`, `intranet/routes/admin_settings.py`, `intranet/jobs/worker.py`, runtime+migration legal-hold DB triggers |

## Priority 1 - High Value Operational Completeness

| ID | Checklist item | Status | Notes |
|---|---|---|---|
| P1-B1 | Optional portal MFA | done | `intranet/routes/portal.py` login MFA enforcement + portal admin MFA enable/rotate/disable controls |
| P1-B2 | Data residency controls (storage/backups/logs/keys) | done | `intranet/policies/residency.py` + enforcement hooks in DMS/matters/portal/billing export and backup routes |
| P1-B3 | Full-text ranked DMS search + matter-scoped search UX | done | `intranet/routes/dms.py` matter-scoped ranked search (`q`, type/confidentiality filters) + saved-search launch links |
| P1-B4 | Email capture access audit and attachment retrieval UX | done | `intranet/routes/dms.py` (`/email-capture/<id>/attachment`) + `email_capture_attachment_access` audit event |
| P1-B5 | Fee arrangements executable billing logic (fixed/capped/blended) | done | `intranet/services/billing_engine.py` fee arrangement adjustments + `intranet/routes/billing.py` admin workflow |
| P1-B6 | Strict maker-checker trust workflow | done | `TrustApprovalRequest` workflow in `intranet/routes/trust_accounting.py` for high-value disbursement/transfer approval |

## Priority 2 - Expansion / Optional Features

| ID | Checklist item | Status | Notes |
|---|---|---|---|
| P2-C1 | Offline time capture + later sync | done | `intranet/routes/timekeeping.py` (`/time/offline-sync`) |
| P2-C2 | Advanced forecast model from stage/history | done | `intranet/jobs/worker.py` (`workload_forecast`) + scheduled execution in `intranet/jobs/scheduler.py` |
| P2-C3 | Automated burnout heuristics generation | done | `intranet/jobs/worker.py` (`burnout_heuristics`) + scheduled execution in `intranet/jobs/scheduler.py` |

## Acceptance Gates for Next Iteration

1. Add PostgreSQL RLS policies with session-scoped identity context for: `matter`, `document_*`, `time_entry`, `invoice*`, `trust_*`. (done)
2. Enforce legal hold restrictions in every archive/delete route and background retention job. (done)
3. Add end-to-end tests for portal link expiry and one-time use, billing adjustments, AR aging buckets, and DMS email dedup. (done)
4. Update deployment docs with any new scheduler job types and expected operational alerts.
