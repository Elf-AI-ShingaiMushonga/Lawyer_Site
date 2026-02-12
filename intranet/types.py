from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ApprovalState(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVERSED = "reversed"


class DocumentState(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FINAL = "final"
    FILED = "filed"


class LedgerEntryType(StrEnum):
    DEPOSIT = "deposit"
    DISBURSEMENT = "disbursement"
    TRANSFER = "transfer"
    REVERSAL = "reversal"


class PortalVisibilityLevel(StrEnum):
    HIDDEN = "hidden"
    SUMMARY_ONLY = "summary_only"
    SHARED_DOCS = "shared_docs"
    FULL_CURATED = "full_curated"


@dataclass(slots=True)
class AccessDecision:
    allow: bool
    deny_reason: str | None = None
    ethical_wall_hit: bool = False
    scope_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class DeadlineCalculationTrace:
    matter_id: int
    trigger_event_id: int | None
    rule_name: str
    base_date_iso: str
    offset_days: int
    adjusted_for_business_day: bool
    holiday_adjustments: int
    result_due_at_iso: str


@dataclass(slots=True)
class LedgerPostingResult:
    posted: bool
    entry_id: int | None = None
    balance_after: float | None = None
    message: str = ""


@dataclass(slots=True)
class InvoiceBuildResult:
    invoice_id: int | None
    line_count: int
    subtotal: float
    tax_total: float
    total: float


@dataclass(slots=True)
class ConflictReport:
    conflict_check_id: int | None
    status: str
    matched_entities: list[str]
    notes: str


@dataclass(slots=True)
class NotificationBatchResult:
    queued_jobs: int
    channels: list[str]


@dataclass(slots=True)
class AnalyticsSnapshot:
    as_of_date: str
    utilization: float
    realization: float
    effective_hourly_rate: float
    metrics: dict[str, float]
