# UX/UI Checklist

This checklist is the default review standard for UI changes in the Law Firm Intranet.

## 1. Navigation and Orientation

- Primary navigation clearly shows current location (`is-active` state).
- Every management page has an obvious "Back" or "Return" action.
- High-frequency actions are visible above the fold.
- Critical workflows avoid dead-ends and provide a path back to a parent page.

## 2. Visual Hierarchy and Readability

- Each page has a clear title and one-line page intro.
- Related fields are grouped in sections (`form-section`) with consistent labels.
- Pages use consistent action hierarchy:
  - primary action: `btn btn-light`
  - secondary action: `btn btn-outline-light`
- Dense data is rendered in `table-responsive` containers.
- Empty states are explicit and instructive.

## 3. Forms and Data Entry

- All interactive inputs have visible labels (`form-label`).
- Optional fields are clearly marked as optional.
- Inputs provide hints for expected format when needed (`form-help`).
- Forms that mutate state include CSRF tokens.
- Validation failure messages are shown as flash alerts and are actionable.

## 4. Tables and Lists

- Column headings are explicit and match domain terms.
- Dates/times are rendered in a consistent format.
- IDs are supplemented with human-readable labels where available.
- Row actions are grouped and scannable (`table-actions`).
- On mobile, table content remains accessible via horizontal scrolling.

## 5. Mobile UX (<=576px)

- Navigation remains usable without overlap or hidden controls.
- Buttons and controls have comfortable tap targets.
- Multi-action rows wrap cleanly and do not clip text.
- Cards, forms, and tables use reduced spacing without becoming cramped.
- No horizontal page-level overflow (excluding intentional table/nav scrollers).

## 6. Accessibility Basics

- Keyboard focus states are visible for links, buttons, and inputs.
- Important controls are reachable in a logical tab order.
- Decorative effects do not block interaction.
- Contrast remains readable for text, badges, and buttons.
- Form controls have associated labels or accessible alternatives.

## 7. Governance and Audit UX

- Admin, portal, and analytics screens show enough context for decisions.
- Risk/compliance pages expose status clearly (active/released/open/closed).
- Operational actions (revoke, release, disable, rotate) are explicit and hard to confuse.

## 8. Release Verification

- Run `python3 -m compileall intranet`.
- Run `PYTHONPATH=. pytest -q`.
- Validate templates parse cleanly in app context.
- Manually spot-check:
  - one admin page
  - one portal page
  - one analytics page
  - one matter workflow page
