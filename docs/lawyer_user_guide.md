# Lawyer User Guide

This guide explains how to use the Law Firm Intranet as a lawyer in day-to-day practice.

## 1. Getting Started

1. Open the site and sign in with your firm email and password.
2. Complete MFA verification (authenticator code or backup code).
3. Confirm you can access the main navigation: `Matters`, `Calendar`, `Workflow`, `DMS`, `Billing`, `Expenses`, `Trust Acct`, `CRM`, `Portal`, `Analytics`.

If login succeeds but pages are blocked, your matter access scope may be restricted by ethical-wall or matter-team permissions.

## 2. Daily Workflow (Recommended)

1. Start on `Dashboard` to review snapshots and alerts.
2. Open `Calendar` and acknowledge critical deadlines.
3. Check `Workflow` for due tasks, approvals, and dependencies.
4. Enter time in `Timekeeping` as work is performed.
5. Upload and finalize documents in `DMS`.
6. Review `Billing` and `Trust Acct` status before end of day.

## 3. Matter Management

Use `Matters` to search, sort, and filter your matter portfolio.

1. Create matters from intake where needed.
2. Open a matter workspace to review:
   - parties,
   - notes,
   - stage history,
   - deadlines,
   - linked documents,
   - open tasks.
3. Keep stage, risk, and matter updates current.
4. Close matters only after checklist completion and legal-hold validation.

## 4. Calendar and Deadlines

Use `Calendar` views (`My`, `Team`, `Matter`) to manage obligations.

1. Monitor deadlines by status (open/acknowledged/overdue).
2. Acknowledge deadlines once ownership is clear.
3. Use override actions only with a documented reason.
4. Escalated deadlines should be resolved first each day.

## 5. Tasks and Workflow

Use `Workflow` for task execution and delegation.

1. Create tasks directly or from templates.
2. Assign one task to multiple assignees when collaboration is required.
3. Use dependencies/checklists to enforce sequence and quality gates.
4. Submit approval-required tasks for review before completion.

## 6. Document Management (DMS)

Use `DMS` for matter-centric document control.

1. Upload documents into the correct matter.
2. Maintain version history; avoid replacing files outside version control.
3. Lock documents during active drafting.
4. Set document state properly: draft, reviewed, final, filed.
5. Use OCR/search to locate content quickly.
6. Build productions and Bates ranges for disclosure workflows.

## 7. Time, Expenses, and Billing

### Timekeeping

1. Use timers for active work.
2. Add clear narratives for each entry.
3. Submit entries for review and lock once billed.

### Expenses

1. Capture disbursements with receipt upload.
2. Mark reimbursable items correctly.
3. Route for approval before invoicing.

### Billing

1. Generate invoices by matter and period.
2. Review line items and adjustments before approval.
3. Export invoice artifacts (PDF/LEDES) as required.
4. Track settled vs pending payments in invoice detail views.

## 8. Trust Accounting (Where Authorized)

Use `Trust Acct` for client fund workflows.

1. Post deposits/disbursements/transfers to the correct client ledger.
2. Do not bypass reconciliation workflows.
3. Review cashbook, trial balance, and auditor/trust reports regularly.
4. Use reversal flows for corrections; do not edit historical ledger entries directly.

## 9. CRM, Intake, and Conflict Checks

Use `CRM` to move leads through intake safely.

1. Create lead and intake records with all known entities.
2. Run conflict checks from intake.
3. Review both direct and semantic OCR-based conflict evidence.
4. Treat semantic hits as review-required evidence, not automatic disqualification.
5. Record override reasons clearly if proceeding after a flagged conflict.

Note: semantic conflict scans run asynchronously. If a scan is marked queued for too long, ask operations to verify the background worker service.

## 10. Client Portal

Use `Portal` to collaborate with clients in a controlled way.

1. Share only approved matter artifacts.
2. Use portal messaging for client-visible updates.
3. Review uploads promptly and move relevant files into DMS workflows.
4. Track invoices/payments exposed to portal users.

## 11. Integrations and Mobile

Use `Integrations` and `Mobile` pages for operational exports/imports.

1. Office365 integrations support calendar/data exchange.
2. Third-party import/export supports cost recovery and conveyancing workflows.
3. Mobile hub supports quick fee/task capture.

## 12. Security and Compliance Rules

1. Never share login credentials or MFA codes.
2. Keep MFA backup codes offline and secure.
3. Do not access matters outside your assignment scope.
4. Respect legal holds and retention controls.
5. Use in-app exports only when required and auditable.

## 13. Common Issues

### I cannot access a matter

Possible causes:
- not on matter team,
- ethical-wall deny,
- revoked session.

Action: contact your administrator to verify matter membership and access policy.

### MFA code fails repeatedly

Action:
1. verify device time sync,
2. use a backup code,
3. if locked out, request admin MFA reset.

### Conflict scan remains queued

Action: ask operations to check worker service and job queue health.

### I cannot upload a document

Possible causes:
- file size exceeds policy,
- file type blocked,
- matter access denied.

Action: reduce file size, confirm file type, and retry with correct matter access.

## 14. Good Practice Checklist

Use this before end-of-day:

1. All critical deadlines acknowledged or escalated.
2. Time entries complete and descriptive.
3. Key documents versioned and correctly staged.
4. New conflict checks reviewed with evidence.
5. Billing/trust items requiring action addressed.
