# Law Firm OS Checklist Traceability

Date: 2026-02-12

This matrix maps the functionality checklist modules (A-L) to implementation artifacts in this repository.

## A. Platform, Security, Compliance
- Authentication/session hardening:
  - `intranet/routes/auth.py`
  - `intranet/routes/auth_plus.py`
  - `intranet/mfa.py`
  - `intranet/security.py` (`enforce_active_session` for idle timeout + revoked-session enforcement)
  - `intranet/helpers.py` (`register_user_session`, `validate_user_session`, `revoke_current_session`, trusted-device upsert/revoke)
  - `intranet/models.py` (`User`, `UserSession`, `TrustedDevice`, `UserMFABackupCode`, SSO models)
  - `intranet/routes/auth_plus.py` (`/auth/sessions`, `/auth/devices/<id>/revoke`)
- Authorization, ACL, ethical walls:
  - `intranet/policies/access.py`
  - `intranet/models.py` (`PermissionGrant`, `EthicalWall*`)
- PostgreSQL row-level security strategy:
  - `intranet/db_context.py` (request/worker session variables for DB access context)
  - `intranet/schema_sync.py` (`_apply_postgres_rls_policies`)
  - `migrations/versions/f7e9c8b1a0f4_full_os_modules_foundation.py` (`_apply_postgres_rls_policies`)
- Audit and monitoring:
  - `intranet/helpers.py` (`audit`)
  - `migrations/versions/f7e9c8b1a0f4_full_os_modules_foundation.py` (append-only audit trigger)
  - `intranet/models.py` (`SuspiciousActivityAlert`, `Notification`)
- Governance controls (retention/legal hold/residency):
  - `intranet/models.py` (`RetentionPolicy`, `LegalHold`, `DataResidencyPolicy`)
  - `intranet/routes/admin_settings.py`
  - `intranet/routes/matters_plus.py` (`/matters/<id>/close` legal-hold archival block)
  - `intranet/jobs/worker.py` (`retention_archive_sweep`)
  - `intranet/policies/residency.py` + route-level enforcement in DMS/portal/billing/backup flows
- Operational reliability:
  - `intranet/routes/ops_plus.py`
  - encrypted backup artifacts with key-verified restore checks (`BACKUP_ENCRYPTION_KEY`, AES-256-GCM)
  - `intranet/models.py` (`BackupRun`, `RestoreVerification`, `DRTarget`)

## B. Matter and Case Management
- Intake, metadata, assignments:
  - `intranet/routes/matters_plus.py` (`/matters/intake`)
  - `intranet/models.py` (`Matter` expanded fields)
- Contacts/parties/relationships:
  - `intranet/models.py` (`Entity`, `MatterParty`, `EntityRelationship`)
  - `intranet/routes/matters_plus.py` (`/matters/<id>/parties`)
- Workspace/timeline/notes:
  - `intranet/routes/matters_plus.py`
  - `intranet/routes/matters.py`
- Stage and closure workflow:
  - `intranet/models.py` (`MatterStageHistory`, `MatterClosingChecklistItem`)
  - `intranet/routes/matters_plus.py` (`/stage`, `/close`)

## C. Docketing and Calendaring
- Calendar views and deadlines:
  - `intranet/routes/calendaring.py`
  - `intranet/models.py` (`Deadline`, `DeadlineRule`, `HolidayCalendar`)
  - `intranet/templates/calendar/matter.html` (manual deadline + hearing scheduling UX)
- Rule-based calculation trace:
  - `intranet/services/deadline_engine.py`

## D. Task and Workflow Management
- Templates/dependencies/checklists/approvals/recurrence:
  - `intranet/routes/workflow.py`
  - `intranet/models.py` (`Task` expanded + workflow tables)
- Automation triggers:
  - `intranet/routes/matters_plus.py` (`matter_stage_changed` notifications on stage transitions)
  - `intranet/routes/dms.py` (`document_uploaded` notifications on version upload)

## E. DMS
- Repository + metadata + versioning + states + locks:
  - `intranet/routes/dms.py`
  - `intranet/models.py` (`DocumentRecord`, `DocumentVersion`, `DocumentLock`...)
- Search/OCR/productions/Bates/email capture:
  - `intranet/models.py` (`DocumentOCRText`, `SavedSearch`, `ProductionSet`, `BatesRange`, `EmailCapture`)
  - `intranet/routes/dms.py`
- Ranked matter-scoped DMS search:
  - `intranet/routes/dms.py` (`/matters/<id>/dms` query ranking with OCR/metadata filters)
  - `intranet/templates/dms/matter_dms.html`
  - `intranet/templates/dms/saved_searches.html`
- Email capture attachment access/download audit:
  - `intranet/routes/dms.py` (`/email-capture/<id>/attachment`, `email_capture_attachment_access`)

## F. Timekeeping
- Timers, entries, policy validation, review/lock:
  - `intranet/routes/timekeeping.py`
  - `intranet/models.py` (`TimeTimer`, `TimeEntry`, `TimeRoundingPolicy`, `TimeValidationEvent`)
- Optional offline capture sync:
  - `intranet/routes/timekeeping.py` (`/time/offline-sync`)

## G. Billing and Invoicing
- Rate cards, invoice generation, approvals, PDF, LEDES:
  - `intranet/services/billing_engine.py`
  - `intranet/routes/billing.py`
  - `intranet/reports/ledes.py`
  - `intranet/models.py` (`Invoice*`, `RateCard`, `LEDESExport`, `PaymentAllocation`)
- Optional fee arrangements (fixed/capped/blended):
  - `intranet/models.py` (`FeeArrangement`)
  - `intranet/services/billing_engine.py` (fee-adjustment application)
  - `intranet/routes/billing.py` (`/billing/rates` fee arrangement admin form)
- Write-down/write-off + AR aging:
  - `intranet/routes/billing.py` (`/billing/invoices/<id>/adjust`, `/billing/ar-aging`)
  - `intranet/models.py` (`InvoiceAdjustment`, `ARSnapshot`)

## H. Expenses
- Capture, receipt handling, approval, invoicing linkage:
  - `intranet/routes/expenses.py`
  - `intranet/models.py` (`ExpenseEntry`)

## I. Trust Accounting
- Ledger posting and overdraft lock:
  - `intranet/services/trust_engine.py`
  - `intranet/routes/trust_accounting.py`
- Optional maker-checker workflow:
  - `intranet/models.py` (`TrustApprovalRequest`)
  - `intranet/routes/trust_accounting.py` (`/trust/approvals/<id>/decision`)
- Reconciliation and reporting:
  - `intranet/reports/trust.py`
  - `intranet/models.py` (`TrustReconciliationRun`, alerts)

## J. CRM and Intake
- Lead pipeline, follow-ups, conflict checks, engagement signing:
  - `intranet/routes/crm.py`
  - `intranet/services/conflict_engine.py`
  - `intranet/models.py` (`CRMLead`, `IntakeForm`, `ConflictCheck`, `EngagementLetter`)
- Conflict report export:
  - `intranet/routes/crm.py` (`/crm/conflicts/<id>/export`)
  - `intranet/reports/conflicts.py`

## K. Client Portal
- Portal auth, scoped access, messages, uploads, invoices, payments:
  - `intranet/routes/portal.py`
  - `intranet/models.py` (`Portal*` tables)
- Optional portal MFA:
  - `intranet/routes/portal.py` (`/portal/login` TOTP check for MFA-enabled portal users)
  - `intranet/templates/portal/admin_users.html` (admin MFA controls)
- Time-limited portal links:
  - `intranet/routes/portal.py` (`/portal/links`, `/portal/link/<token>`)
  - `intranet/models.py` (`PortalLinkToken`)
  - one-time use + expiry enforcement at access time

## L. HR, Capacity, Analytics
- Utilization, realization, EHR, workload, profitability, forecast, burnout:
  - `intranet/services/analytics_engine.py`
  - `intranet/routes/analytics.py`
  - `intranet/models.py` (`AnalyticsMetricSnapshot`, `WorkloadForecast`, `BurnoutSignal`)
  - `intranet/jobs/worker.py` (`workload_forecast`, `burnout_heuristics`)

## Jobs and Automation
- Queue, worker, scheduler:
  - `intranet/jobs/queue.py`
  - `intranet/jobs/worker.py`
  - `intranet/jobs/scheduler.py`
  - `intranet/models.py` (`JobQueue`, `JobHistory`, `ScheduledJob`)
  - `app.py` (`worker`, `scheduler` commands)
- Deadline escalations + digests:
  - `intranet/jobs/worker.py` (`deadline_escalation_scan`, `deadline_digest`)
  - `intranet/jobs/scheduler.py` (`DEFAULT_PERIODIC_JOBS`)
- Analytics automation:
  - `intranet/jobs/worker.py` (`analytics_snapshot`, `workload_forecast`, `burnout_heuristics`)
  - `intranet/jobs/scheduler.py` (`DEFAULT_PERIODIC_JOBS`)

## Tests
- Service invariants:
  - `tests/test_services.py`
- Reporting format:
  - `tests/test_reports.py`
- Route coverage:
  - `tests/test_routes.py`
- Cross-module relationship and remediation regression:
  - `tests/test_relationship_integrity.py`
  - includes portal link expiry + one-time reuse block, calendar creation flows, revoked/expired session enforcement, trusted-device revoke
