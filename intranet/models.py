from __future__ import annotations

import datetime as dt
from .timeutils import utc_now

from flask_login import UserMixin
from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


# ---------------------------------------------------------------------------
# Core and Existing Models
# ---------------------------------------------------------------------------


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=utc_now)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(120), nullable=False)
    entity_type = db.Column(db.String(40), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    details_json = db.Column(db.Text, nullable=True)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255), nullable=False, default="(Unnamed)")
    role = db.Column(db.String(40), nullable=False, default="junior_attorney")
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_login_at = db.Column(db.DateTime, nullable=True)

    # Security hardening
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret = db.Column(db.String(64), nullable=True)
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_failed_login_at = db.Column(db.DateTime, nullable=True)
    password_changed_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def get_id(self):
        return str(self.id)


class DirectorTeamMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    director_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    member_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("director_id", "member_user_id", name="uq_director_team_member"),
        db.UniqueConstraint("member_user_id", name="uq_director_team_member_user"),
        db.Index("ix_director_team_director_member", "director_id", "member_user_id"),
    )


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Matter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_no = db.Column(db.String(80), unique=True, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    client_name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(40), nullable=False, default="Open")
    description = db.Column(db.Text, nullable=True)
    objective = db.Column(db.Text, nullable=True)
    risk_level = db.Column(db.String(40), nullable=False, default="Medium")
    budget_status = db.Column(db.String(60), nullable=False, default="On Track")
    outcome_summary = db.Column(db.Text, nullable=True)
    last_update_note = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    closed_at = db.Column(db.DateTime, nullable=True)
    last_updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    legal_category = db.Column(db.String(120), nullable=True, index=True)
    archetype_id = db.Column(db.Integer, db.ForeignKey("matter_template.id"), nullable=True, index=True)
    archetype_data_json = db.Column(db.Text, nullable=True)

    # Expanded matter metadata
    court_name = db.Column(db.String(255), nullable=True)
    judge_name = db.Column(db.String(255), nullable=True)
    jurisdiction = db.Column(db.String(80), nullable=True)
    stage = db.Column(db.String(80), nullable=True)
    practice_area = db.Column(db.String(120), nullable=True)
    case_type = db.Column(db.String(120), nullable=True)
    risk_taxonomy = db.Column(db.String(120), nullable=True)
    archival_status = db.Column(db.String(40), nullable=True, default="active")
    archival_due_at = db.Column(db.DateTime, nullable=True)
    closing_checklist_json = db.Column(db.Text, nullable=True)
    originating_partner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    supervising_partner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class MatterMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role_in_matter = db.Column(db.String(80), nullable=False, default="Team")
    __table_args__ = (
        db.UniqueConstraint("matter_id", "user_id", name="uq_matter_user"),
        db.Index("ix_matter_member_user_matter", "user_id", "matter_id"),
    )


class MatterPin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("user_id", "matter_id", name="uq_matter_pin_user_matter"),
        db.Index("ix_matter_pin_user_created", "user_id", "created_at"),
    )


class MatterRecentView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    first_viewed_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_viewed_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    view_count = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.UniqueConstraint("user_id", "matter_id", name="uq_matter_recent_user_matter"),
        db.Index("ix_matter_recent_user_last_viewed", "user_id", "last_viewed_at"),
    )


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="Todo")
    due_date = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    # Workflow expansion
    priority = db.Column(db.String(20), nullable=False, default="Medium")
    sla_hours = db.Column(db.Integer, nullable=True)
    approval_state = db.Column(db.String(20), nullable=False, default="draft")
    requires_two_person_review = db.Column(db.Boolean, nullable=False, default=False)
    recurrence_rule = db.Column(db.String(120), nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.Index("ix_task_assigned_status_due", "assigned_to", "status", "due_date"),
        db.Index("ix_task_matter_status_due", "matter_id", "status", "due_date"),
    )


class TaskAssignee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("task_id", "user_id", name="uq_task_assignee_task_user"),
        db.Index("ix_task_assignee_user_task", "user_id", "task_id"),
    )


class MatterTimelineEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    event_date = db.Column(db.Date, nullable=False)
    event_type = db.Column(db.String(40), nullable=False, default="Milestone")
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_milestone = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class MatterActivity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(120), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class DocumentFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    content_type = db.Column(db.String(120), nullable=True)
    category = db.Column(db.String(80), nullable=True)
    doc_version = db.Column(db.String(40), nullable=True)
    lifecycle_stage = db.Column(db.String(40), nullable=False, default="Draft")
    owner_name = db.Column(db.String(255), nullable=True)
    is_privileged = db.Column(db.Boolean, nullable=False, default=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    organization = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class KnowledgeBase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    tags = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TenderOpportunity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference_no = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    issuing_authority = db.Column(db.String(255), nullable=False)
    province = db.Column(db.String(80), nullable=False, default="National")
    sector = db.Column(db.String(120), nullable=True)
    tender_type = db.Column(db.String(60), nullable=False, default="Tender")
    portal_source = db.Column(db.String(120), nullable=False, default="SA eTender Portal")
    status = db.Column(db.String(40), nullable=False, default="Sourced")
    etender_url = db.Column(db.String(500), nullable=True)
    briefing_required = db.Column(db.Boolean, nullable=False, default=False)
    briefing_date = db.Column(db.DateTime, nullable=True)
    closing_at = db.Column(db.DateTime, nullable=False, index=True)
    validity_end_date = db.Column(db.Date, nullable=True)
    estimated_value = db.Column(db.Float, nullable=True)
    preference_system = db.Column(db.String(40), nullable=True)
    cidb_required = db.Column(db.Boolean, nullable=False, default=False)
    cidb_grading = db.Column(db.String(40), nullable=True)
    local_content_required = db.Column(db.Boolean, nullable=False, default=False)
    submission_channel = db.Column(db.String(80), nullable=True)
    submission_address = db.Column(db.Text, nullable=True)
    contact_person = db.Column(db.String(255), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    contact_phone = db.Column(db.String(80), nullable=True)
    csd_supplier_number = db.Column(db.String(120), nullable=True)
    tcs_pin = db.Column(db.String(120), nullable=True)
    bbbee_level = db.Column(db.String(80), nullable=True)
    bid_manager_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    scope_summary = db.Column(db.Text, nullable=True)
    next_action = db.Column(db.Text, nullable=True)
    internal_notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_tender_opportunity_status_closing", "status", "closing_at"),
        db.Index("ix_tender_opportunity_bid_manager_status", "bid_manager_user_id", "status"),
    )


class TenderChecklistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tender_id = db.Column(db.Integer, db.ForeignKey("tender_opportunity.id"), nullable=False, index=True)
    item_key = db.Column(db.String(80), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    is_required = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(30), nullable=False, default="pending")
    notes = db.Column(db.Text, nullable=True)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("tender_id", "item_key", name="uq_tender_checklist_item"),
        db.Index("ix_tender_checklist_tender_status", "tender_id", "status"),
    )


class GovernanceIncident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    incident_type = db.Column(db.String(60), nullable=False, default="Incident")
    severity = db.Column(db.String(40), nullable=False, default="Medium")
    status = db.Column(db.String(40), nullable=False, default="Open")
    summary = db.Column(db.Text, nullable=False)
    impact = db.Column(db.Text, nullable=True)
    resolution = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class ITAsset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_tag = db.Column(db.String(80), nullable=False, unique=True, index=True)
    name = db.Column(db.String(180), nullable=False)
    asset_type = db.Column(db.String(60), nullable=False, default="laptop")
    status = db.Column(db.String(40), nullable=False, default="in_stock")
    serial_number = db.Column(db.String(120), nullable=True)
    vendor = db.Column(db.String(180), nullable=True)
    location = db.Column(db.String(180), nullable=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    purchase_date = db.Column(db.Date, nullable=True)
    warranty_expires_on = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_it_asset_status_updated", "status", "updated_at"),
    )


class HelpdeskTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_no = db.Column(db.String(40), nullable=False, unique=True, index=True)
    subject = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(60), nullable=False, default="general")
    priority = db.Column(db.String(20), nullable=False, default="medium")
    status = db.Column(db.String(40), nullable=False, default="new")
    reporter_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("it_asset.id"), nullable=True, index=True)
    first_response_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolution_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_helpdesk_ticket_status_updated", "status", "updated_at"),
        db.Index("ix_helpdesk_ticket_reporter_status", "reporter_user_id", "status"),
    )


class HelpdeskTicketComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("helpdesk_ticket.id"), nullable=False, index=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_helpdesk_ticket_comment_ticket_created", "ticket_id", "created_at"),
    )


# ---------------------------------------------------------------------------
# Identity, Sessions, and SSO-like Federation
# ---------------------------------------------------------------------------


class UserSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    session_token_hash = db.Column(db.String(128), nullable=False, unique=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)


class TrustedDevice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    device_name = db.Column(db.String(255), nullable=False)
    fingerprint_hash = db.Column(db.String(128), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class UserMFABackupCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    used_at = db.Column(db.DateTime, nullable=True)


class SSOApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    client_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    client_secret_hash = db.Column(db.String(255), nullable=False)
    redirect_uri = db.Column(db.String(500), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class SSOAuthorizationCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(db.Integer, db.ForeignKey("sso_application.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False, unique=True)
    scope = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=False)
    consumed_at = db.Column(db.DateTime, nullable=True)


class SSOToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    app_id = db.Column(db.Integer, db.ForeignKey("sso_application.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    access_token_hash = db.Column(db.String(128), nullable=False, unique=True)
    refresh_token_hash = db.Column(db.String(128), nullable=True)
    scope = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Authorization, Ethical Walls, Governance Controls
# ---------------------------------------------------------------------------


class PermissionGrant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(40), nullable=False, index=True)
    resource = db.Column(db.String(80), nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    is_allowed = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class EthicalWall(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class EthicalWallRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wall_id = db.Column(db.Integer, db.ForeignKey("ethical_wall.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    is_deny = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("wall_id", "user_id", name="uq_ethical_wall_user_rule"),
        db.Index("ix_ethical_wall_rule_user_state", "user_id", "is_active", "is_deny", "wall_id"),
    )


class EthicalWallMatter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wall_id = db.Column(db.Integer, db.ForeignKey("ethical_wall.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("wall_id", "matter_id", name="uq_ethical_wall_matter"),
        db.Index("ix_ethical_wall_matter_matter_wall", "matter_id", "wall_id"),
    )


class LegalHold(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    released_at = db.Column(db.DateTime, nullable=True)


class RetentionPolicy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    matter_type = db.Column(db.String(120), nullable=True)
    jurisdiction = db.Column(db.String(80), nullable=True)
    retain_days = db.Column(db.Integer, nullable=False)
    archive_after_days = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class DataResidencyPolicy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    data_class = db.Column(db.String(80), nullable=False)
    region_code = db.Column(db.String(40), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class SuspiciousActivityAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    alert_type = db.Column(db.String(80), nullable=False)
    severity = db.Column(db.String(40), nullable=False, default="medium")
    status = db.Column(db.String(40), nullable=False, default="open")
    details_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(80), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    subject_ref = db.Column(db.String(255), nullable=False)
    channel = db.Column(db.String(40), nullable=False, default="in_app")
    status = db.Column(db.String(40), nullable=False, default="queued")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    delivered_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Admin Configuration and Templates
# ---------------------------------------------------------------------------


class FirmSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(120), nullable=False, unique=True)
    setting_value_json = db.Column(db.Text, nullable=False, default="{}")
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class Office(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    jurisdiction = db.Column(db.String(80), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PracticeArea(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TimekeeperRole(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class MatterTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    legal_category = db.Column(db.String(120), nullable=True)
    practice_area = db.Column(db.String(120), nullable=True)
    default_stage = db.Column(db.String(80), nullable=True)
    default_risk_level = db.Column(db.String(40), nullable=True)
    checklist_json = db.Column(db.Text, nullable=True)
    required_fields_json = db.Column(db.Text, nullable=True)
    boilerplate_template = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ContractTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    legal_category = db.Column(db.String(120), nullable=True)
    archetype_id = db.Column(db.Integer, db.ForeignKey("matter_template.id"), nullable=True, index=True)
    contract_type = db.Column(db.String(80), nullable=False, default="Contract")
    required_fields_json = db.Column(db.Text, nullable=True)
    body = db.Column(db.Text, nullable=False)
    requires_signature = db.Column(db.Boolean, nullable=False, default=True)
    auto_create_on_matter_open = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_contract_template_archetype_active", "archetype_id", "is_active"),
    )


class TaskTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    matter_type = db.Column(db.String(120), nullable=True)
    priority = db.Column(db.String(20), nullable=False, default="Medium")
    sla_hours = db.Column(db.Integer, nullable=True)
    recurrence_rule = db.Column(db.String(120), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TaskTemplateItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_template_id = db.Column(db.Integer, db.ForeignKey("task_template.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    position = db.Column(db.Integer, nullable=False, default=1)


class DocumentTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    archetype_id = db.Column(db.Integer, db.ForeignKey("matter_template.id"), nullable=True, index=True)
    template_type = db.Column(db.String(80), nullable=False)
    body = db.Column(db.Text, nullable=False)
    requires_signature = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Matter Parties, Notes, and Stage Tracking
# ---------------------------------------------------------------------------


class Entity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=False, default="organization")
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(80), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class EntityRelationship(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    src_entity_id = db.Column(db.Integer, db.ForeignKey("entity.id"), nullable=False, index=True)
    dst_entity_id = db.Column(db.Integer, db.ForeignKey("entity.id"), nullable=False, index=True)
    relationship_type = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class MatterParty(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    entity_id = db.Column(db.Integer, db.ForeignKey("entity.id"), nullable=False, index=True)
    party_role = db.Column(db.String(80), nullable=False)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class MatterNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    tags = db.Column(db.String(255), nullable=True)
    privilege_label = db.Column(db.String(80), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class MatterNoteACL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey("matter_note.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    can_read = db.Column(db.Boolean, nullable=False, default=True)
    can_edit = db.Column(db.Boolean, nullable=False, default=False)
    __table_args__ = (db.UniqueConstraint("note_id", "user_id", name="uq_note_acl_user"),)


class MatterStageHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    from_stage = db.Column(db.String(80), nullable=True)
    to_stage = db.Column(db.String(80), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    changed_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    changed_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class MatterClosingChecklistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    item_text = db.Column(db.String(255), nullable=False)
    is_done = db.Column(db.Boolean, nullable=False, default=False)
    done_at = db.Column(db.DateTime, nullable=True)
    done_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class MatterWorkspaceDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(40), nullable=False, default="draft")
    template_id = db.Column(db.Integer, db.ForeignKey("document_template.id"), nullable=True, index=True)
    document_type = db.Column(db.String(80), nullable=True)
    confidentiality = db.Column(db.String(80), nullable=True)
    privilege_label = db.Column(db.String(80), nullable=True)
    retention_category = db.Column(db.String(80), nullable=True)
    legal_hold = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    last_edited_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    published_document_id = db.Column(db.Integer, db.ForeignKey("document_record.id"), nullable=True, index=True)
    published_version_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_published_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.Index("ix_matter_workspace_document_matter_updated", "matter_id", "updated_at"),
        db.Index("ix_matter_workspace_document_matter_status", "matter_id", "status"),
    )


class MatterWorkspaceDocumentComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_document_id = db.Column(
        db.Integer,
        db.ForeignKey("matter_workspace_document.id"),
        nullable=False,
        index=True,
    )
    anchor_label = db.Column(db.String(120), nullable=True)
    body = db.Column(db.Text, nullable=False)
    is_resolved = db.Column(db.Boolean, nullable=False, default=False)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index(
            "ix_matter_workspace_document_comment_document_created",
            "workspace_document_id",
            "created_at",
        ),
    )


class MatterWorkspaceDocumentPresence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workspace_document_id = db.Column(
        db.Integer,
        db.ForeignKey("matter_workspace_document.id"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    state = db.Column(db.String(40), nullable=False, default="viewing")
    cursor_label = db.Column(db.String(120), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_document_id",
            "user_id",
            name="uq_matter_workspace_document_presence_user",
        ),
        db.Index(
            "ix_matter_workspace_document_presence_document_seen",
            "workspace_document_id",
            "last_seen_at",
        ),
    )


# ---------------------------------------------------------------------------
# Docketing and Calendaring
# ---------------------------------------------------------------------------


class HolidayCalendar(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    jurisdiction = db.Column(db.String(80), nullable=True)
    office_id = db.Column(db.Integer, db.ForeignKey("office.id"), nullable=True)
    holiday_date = db.Column(db.Date, nullable=False, index=True)
    label = db.Column(db.String(120), nullable=False)


class DeadlineRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    jurisdiction = db.Column(db.String(80), nullable=True)
    office_id = db.Column(db.Integer, db.ForeignKey("office.id"), nullable=True)
    trigger_type = db.Column(db.String(80), nullable=False)
    offset_days = db.Column(db.Integer, nullable=False, default=0)
    business_day_adjust = db.Column(db.Boolean, nullable=False, default=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class Deadline(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=False)
    due_at = db.Column(db.Date, nullable=False, index=True)
    is_critical = db.Column(db.Boolean, nullable=False, default=False)
    source_rule_id = db.Column(db.Integer, db.ForeignKey("deadline_rule.id"), nullable=True)
    calculation_trace = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="open")
    acknowledged_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    acknowledged_at = db.Column(db.DateTime, nullable=True)
    override_reason = db.Column(db.Text, nullable=True)
    overridden_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    overridden_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Workflow Extensions
# ---------------------------------------------------------------------------


class TaskDependency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    depends_on_task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (db.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency"),)


class TaskChecklistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    item_text = db.Column(db.String(255), nullable=False)
    is_done = db.Column(db.Boolean, nullable=False, default=False)
    position = db.Column(db.Integer, nullable=False, default=1)


class TaskApproval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    requested_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approver_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    state = db.Column(db.String(20), nullable=False, default="pending")
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    decided_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# DMS Normalized Records
# ---------------------------------------------------------------------------


class DocumentRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    document_type = db.Column(db.String(80), nullable=True)
    confidentiality = db.Column(db.String(80), nullable=True)
    privilege_label = db.Column(db.String(80), nullable=True)
    retention_category = db.Column(db.String(80), nullable=True)
    legal_hold = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index(
            "ix_document_record_matter_type_conf_created",
            "matter_id",
            "document_type",
            "confidentiality",
            "created_at",
        ),
    )


class DocumentVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("document_record.id"), nullable=False, index=True)
    document_file_id = db.Column(db.Integer, db.ForeignKey("document_file.id"), nullable=True, index=True)
    version_no = db.Column(db.Integer, nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    hash_chain_prev = db.Column(db.String(64), nullable=True)
    hash_chain_current = db.Column(db.String(64), nullable=True)
    state = db.Column(db.String(20), nullable=False, default="draft")
    notes = db.Column(db.Text, nullable=True)
    filed_reference = db.Column(db.String(120), nullable=True)
    is_immutable = db.Column(db.Boolean, nullable=False, default=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (db.UniqueConstraint("document_id", "version_no", name="uq_document_version_no"),)


class DocumentLock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("document_record.id"), nullable=False, index=True)
    locked_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    lock_reason = db.Column(db.String(255), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=True)
    released_at = db.Column(db.DateTime, nullable=True)


class DocumentOCRText(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    document_version_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=False, index=True)
    extracted_text = db.Column(db.Text, nullable=False)
    extracted_at = db.Column(db.DateTime, nullable=False, default=utc_now)


@event.listens_for(DocumentOCRText, "before_insert")
@event.listens_for(DocumentOCRText, "before_update")
def _sanitize_document_ocr_text(_mapper, _connection, target) -> None:
    # PostgreSQL rejects NUL (0x00) in text/varchar fields.
    text = target.extracted_text or ""
    target.extracted_text = text.replace("\x00", "")


class SavedSearch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    query_json = db.Column(db.Text, nullable=False)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ProductionSet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    confidentiality_designation = db.Column(db.String(80), nullable=True)
    watermark_text = db.Column(db.String(120), nullable=True)
    bates_prefix = db.Column(db.String(20), nullable=True)
    bates_start = db.Column(db.Integer, nullable=True)
    bates_end = db.Column(db.Integer, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ProductionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    production_set_id = db.Column(db.Integer, db.ForeignKey("production_set.id"), nullable=False, index=True)
    document_version_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=False, index=True)
    bates_number = db.Column(db.String(40), nullable=True, index=True)


class BatesRange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    production_set_id = db.Column(db.Integer, db.ForeignKey("production_set.id"), nullable=False, index=True)
    prefix = db.Column(db.String(20), nullable=False)
    start_no = db.Column(db.Integer, nullable=False)
    end_no = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class EmailCapture(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    message_id_hash = db.Column(db.String(128), nullable=False, index=True)
    dedup_key = db.Column(db.String(128), nullable=True, index=True)
    subject = db.Column(db.String(255), nullable=True)
    sender = db.Column(db.String(255), nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)
    stored_filename = db.Column(db.String(255), nullable=True)
    attachment_hash = db.Column(db.String(64), nullable=True)
    captured_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    captured_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Timekeeping
# ---------------------------------------------------------------------------


class TimeRoundingPolicy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(255), nullable=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True)
    increment_hours = db.Column(db.Float, nullable=False, default=0.1)
    min_narrative_length = db.Column(db.Integer, nullable=False, default=20)
    require_activity_code = db.Column(db.Boolean, nullable=False, default=False)
    daily_hour_cap = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class TimeTimer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True, index=True)
    label = db.Column(db.String(255), nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    paused_at = db.Column(db.DateTime, nullable=True)
    elapsed_seconds = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="paused")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=True, index=True)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=True)
    hours = db.Column(db.Float, nullable=False, default=0.0)
    rounded_hours = db.Column(db.Float, nullable=False, default=0.0)
    narrative = db.Column(db.Text, nullable=True)
    task_code = db.Column(db.String(40), nullable=True)
    activity_code = db.Column(db.String(40), nullable=True)
    is_billable = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(20), nullable=False, default="draft")
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_time_entry_user_start_at", "user_id", "start_at"),
        db.Index("ix_time_entry_matter_start_at", "matter_id", "start_at"),
    )

    matter = db.relationship("Matter", foreign_keys=[matter_id])


class TimeValidationEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time_entry_id = db.Column(db.Integer, db.ForeignKey("time_entry.id"), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Billing and Invoicing
# ---------------------------------------------------------------------------


class RateCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=True)
    client_name = db.Column(db.String(255), nullable=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    timekeeper_role_id = db.Column(db.Integer, db.ForeignKey("timekeeper_role.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    rate_per_hour = db.Column(db.Float, nullable=False, default=0.0)
    effective_from = db.Column(db.Date, nullable=True)
    effective_to = db.Column(db.Date, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class FeeArrangement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    arrangement_type = db.Column(db.String(40), nullable=False, default="hourly")
    fixed_amount = db.Column(db.Float, nullable=True)
    cap_amount = db.Column(db.Float, nullable=True)
    blended_rate = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)


class TaxRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jurisdiction = db.Column(db.String(80), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    rate_percent = db.Column(db.Float, nullable=False, default=0.0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    client_name = db.Column(db.String(255), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    subtotal = db.Column(db.Float, nullable=False, default=0.0)
    tax_total = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    pdf_path = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (db.Index("ix_invoice_matter_created_at", "matter_id", "created_at"),)


class InvoiceLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    time_entry_id = db.Column(db.Integer, db.ForeignKey("time_entry.id"), nullable=True, index=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expense_entry.id"), nullable=True, index=True)
    description = db.Column(db.String(255), nullable=False)
    hours = db.Column(db.Float, nullable=False, default=0.0)
    rate = db.Column(db.Float, nullable=False, default=0.0)
    amount = db.Column(db.Float, nullable=False, default=0.0)
    tax_amount = db.Column(db.Float, nullable=False, default=0.0)
    task_code = db.Column(db.String(40), nullable=True)
    activity_code = db.Column(db.String(40), nullable=True)


class InvoiceAdjustment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    adjustment_type = db.Column(db.String(40), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class LEDESExport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    variant = db.Column(db.String(40), nullable=False, default="1998B")
    file_path = db.Column(db.String(255), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ARSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    as_of_date = db.Column(db.Date, nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    outstanding_amount = db.Column(db.Float, nullable=False, default=0.0)
    aging_bucket = db.Column(db.String(40), nullable=False)
    collection_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PaymentAllocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(40), nullable=True)
    reference = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="settled")
    settled_at = db.Column(db.DateTime, nullable=True)
    settled_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    external_txn_id = db.Column(db.String(120), nullable=True)
    processor_note = db.Column(db.Text, nullable=True)
    allocated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    __table_args__ = (
        db.Index("ix_payment_allocation_invoice_allocated", "invoice_id", "allocated_at"),
        db.Index("ix_payment_allocation_status_settled", "status", "settled_at"),
    )


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


class ExpenseEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    category = db.Column(db.String(80), nullable=False, default="General")
    description = db.Column(db.Text, nullable=True)
    incurred_on = db.Column(db.Date, nullable=False)
    is_reimbursable = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(40), nullable=False, default="draft")
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    receipt_filename = db.Column(db.String(255), nullable=True)
    receipt_sha256 = db.Column(db.String(64), nullable=True)
    receipt_ocr_text = db.Column(db.Text, nullable=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Trust Accounting
# ---------------------------------------------------------------------------


class TrustAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    bank_name = db.Column(db.String(180), nullable=True)
    account_no_last4 = db.Column(db.String(4), nullable=True)
    jurisdiction = db.Column(db.String(80), nullable=True)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TrustClientLedger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trust_account_id = db.Column(db.Integer, db.ForeignKey("trust_account.id"), nullable=False, index=True)
    client_name = db.Column(db.String(255), nullable=False)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    current_balance = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (db.CheckConstraint("current_balance >= 0", name="ck_trust_client_balance_nonnegative"),)


class TrustLedgerEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trust_account_id = db.Column(db.Integer, db.ForeignKey("trust_account.id"), nullable=False, index=True)
    client_ledger_id = db.Column(db.Integer, db.ForeignKey("trust_client_ledger.id"), nullable=False, index=True)
    entry_type = db.Column(db.String(40), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    description = db.Column(db.Text, nullable=True)
    supporting_document_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=True)
    reversal_of_entry_id = db.Column(db.Integer, db.ForeignKey("trust_ledger_entry.id"), nullable=True)
    immutable_ref = db.Column(db.String(120), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_trust_ledger_amount_positive"),
        db.CheckConstraint(
            "entry_type IN ('deposit', 'disbursement', 'transfer', 'reversal')",
            name="ck_trust_ledger_entry_type",
        ),
        db.Index("ix_trust_ledger_entry_reversal_of_entry_id", "reversal_of_entry_id"),
        db.Index("ix_trust_ledger_entry_account_created", "trust_account_id", "created_at"),
    )


class TrustReconciliationRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trust_account_id = db.Column(db.Integer, db.ForeignKey("trust_account.id"), nullable=False, index=True)
    bank_statement_import_id = db.Column(db.Integer, db.ForeignKey("trust_bank_statement_import.id"), nullable=True, index=True)
    period_start = db.Column(db.DateTime, nullable=False)
    period_end = db.Column(db.DateTime, nullable=False)
    bank_closing_balance = db.Column(db.Float, nullable=False, default=0.0)
    ledger_closing_balance = db.Column(db.Float, nullable=False, default=0.0)
    client_subledger_total = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(40), nullable=False, default="draft")
    exception_notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class TrustThresholdAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_ledger_id = db.Column(db.Integer, db.ForeignKey("trust_client_ledger.id"), nullable=False, index=True)
    threshold_amount = db.Column(db.Float, nullable=False)
    current_balance = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="open")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class TrustApprovalRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    action_type = db.Column(db.String(40), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="pending")
    requested_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approved_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    requested_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    approved_at = db.Column(db.DateTime, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    executed_entry_id = db.Column(db.Integer, db.ForeignKey("trust_ledger_entry.id"), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class TrustBankStatementImport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trust_account_id = db.Column(db.Integer, db.ForeignKey("trust_account.id"), nullable=False, index=True)
    statement_label = db.Column(db.String(180), nullable=True)
    source_filename = db.Column(db.String(255), nullable=False)
    period_start = db.Column(db.Date, nullable=True, index=True)
    period_end = db.Column(db.Date, nullable=True, index=True)
    opening_balance = db.Column(db.Float, nullable=True)
    closing_balance = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    row_count = db.Column(db.Integer, nullable=False, default=0)
    imported_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    imported_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    notes = db.Column(db.Text, nullable=True)


class TrustBankStatementLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(db.Integer, db.ForeignKey("trust_bank_statement_import.id"), nullable=False, index=True)
    posted_on = db.Column(db.Date, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=True)
    reference = db.Column(db.String(180), nullable=True)
    debit = db.Column(db.Float, nullable=False, default=0.0)
    credit = db.Column(db.Float, nullable=False, default=0.0)
    signed_amount = db.Column(db.Float, nullable=False, default=0.0)
    running_balance = db.Column(db.Float, nullable=True)
    raw_json = db.Column(db.Text, nullable=True)


class Section86Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trust_account_id = db.Column(db.Integer, db.ForeignKey("trust_account.id"), nullable=False, index=True)
    client_ledger_id = db.Column(db.Integer, db.ForeignKey("trust_client_ledger.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    investment_ref = db.Column(db.String(120), nullable=False, unique=True, index=True)
    institution = db.Column(db.String(180), nullable=True)
    principal_amount = db.Column(db.Float, nullable=False)
    annual_rate_percent = db.Column(db.Float, nullable=False, default=0.0)
    opened_on = db.Column(db.Date, nullable=False, index=True)
    maturity_on = db.Column(db.Date, nullable=True, index=True)
    status = db.Column(db.String(40), nullable=False, default="active")
    source = db.Column(db.String(40), nullable=False, default="manual")
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    closed_on = db.Column(db.Date, nullable=True)


class Section86Accrual(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    investment_id = db.Column(db.Integer, db.ForeignKey("section86_investment.id"), nullable=False, index=True)
    accrual_date = db.Column(db.Date, nullable=False, index=True)
    interest_amount = db.Column(db.Float, nullable=False)
    withholding_tax_amount = db.Column(db.Float, nullable=False, default=0.0)
    net_interest_amount = db.Column(db.Float, nullable=False)
    posted_entry_id = db.Column(db.Integer, db.ForeignKey("trust_ledger_entry.id"), nullable=True, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (db.UniqueConstraint("investment_id", "accrual_date", name="uq_section86_accrual_day"),)


# ---------------------------------------------------------------------------
# CRM and Intake
# ---------------------------------------------------------------------------


class CRMLead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(255), nullable=False)
    organization = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(80), nullable=True)
    source = db.Column(db.String(80), nullable=True)
    stage = db.Column(db.String(40), nullable=False, default="new")
    notes = db.Column(db.Text, nullable=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class CRMFollowUp(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("crm_lead.id"), nullable=False, index=True)
    due_at = db.Column(db.DateTime, nullable=False)
    note = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="open")
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class LeadQuote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("crm_lead.id"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    fee_model = db.Column(db.String(40), nullable=False, default="fixed")
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    estimated_amount = db.Column(db.Float, nullable=False, default=0.0)
    estimated_hours = db.Column(db.Float, nullable=True)
    hourly_rate = db.Column(db.Float, nullable=True)
    disbursement_estimate = db.Column(db.Float, nullable=False, default=0.0)
    tax_rate = db.Column(db.Float, nullable=False, default=15.0)
    scope_summary = db.Column(db.Text, nullable=True)
    assumptions = db.Column(db.Text, nullable=True)
    valid_until = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="draft")
    status_note = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_lead_quote_lead_created", "lead_id", "created_at"),
        db.Index("ix_lead_quote_status_valid", "status", "valid_until"),
    )


class IntakeForm(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("crm_lead.id"), nullable=True, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    data_json = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ConflictCheck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    intake_form_id = db.Column(db.Integer, db.ForeignKey("intake_form.id"), nullable=False, index=True)
    status = db.Column(db.String(40), nullable=False, default="pending")
    result_json = db.Column(db.Text, nullable=True)
    override_required = db.Column(db.Boolean, nullable=False, default=False)
    overridden_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    override_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ConflictSemanticHit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conflict_check_id = db.Column(db.Integer, db.ForeignKey("conflict_check.id"), nullable=False, index=True)
    document_ocr_text_id = db.Column(db.Integer, db.ForeignKey("document_ocr_text.id"), nullable=False, index=True)
    document_version_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=True, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    candidate_entity = db.Column(db.String(255), nullable=False)
    matched_phrase = db.Column(db.String(255), nullable=True)
    match_reason = db.Column(db.String(255), nullable=True)
    similarity_score = db.Column(db.Float, nullable=False, default=0.0)
    lexical_score = db.Column(db.Float, nullable=False, default=0.0)
    vector_score = db.Column(db.Float, nullable=False, default=0.0)
    excerpt = db.Column(db.Text, nullable=True)
    semantic_rank = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_conflict_semantic_hit_conflict_rank", "conflict_check_id", "semantic_rank"),
        db.Index("ix_conflict_semantic_hit_similarity", "similarity_score", "created_at"),
    )


class EngagementLetter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    template_name = db.Column(db.String(120), nullable=True)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="draft")
    signed_by = db.Column(db.String(255), nullable=True)
    signed_at = db.Column(db.DateTime, nullable=True)
    signed_ip = db.Column(db.String(64), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Client Portal
# ---------------------------------------------------------------------------


class PortalUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secret = db.Column(db.String(64), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_login_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class PortalMatterAccess(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    visibility_level = db.Column(db.String(40), nullable=False, default="summary_only")
    granted_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    granted_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    revoked_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.UniqueConstraint("portal_user_id", "matter_id", name="uq_portal_user_matter_access"),
        db.Index("ix_portal_matter_access_user_revoked_matter", "portal_user_id", "revoked_at", "matter_id"),
    )


class PortalMessageThread(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    subject = db.Column(db.String(255), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by_portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PortalMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("portal_message_thread.id"), nullable=False, index=True)
    body = db.Column(db.Text, nullable=False)
    from_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    from_portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PortalUpload(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PortalInvoiceView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    last_viewed_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("portal_user_id", "invoice_id", name="uq_portal_invoice_view_user_invoice"),
        db.Index("ix_portal_invoice_view_user_viewed", "portal_user_id", "last_viewed_at"),
    )


class PortalPaymentReceipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False, index=True)
    portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(8), nullable=False, default="ZAR")
    status = db.Column(db.String(40), nullable=False, default="recorded")
    reference = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class PortalLinkToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    portal_user_id = db.Column(db.Integer, db.ForeignKey("portal_user.id"), nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    document_version_id = db.Column(db.Integer, db.ForeignKey("document_version.id"), nullable=True, index=True)
    token_hash = db.Column(db.String(128), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    used_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Analytics and Capacity
# ---------------------------------------------------------------------------


class AnalyticsMetricSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    as_of_date = db.Column(db.Date, nullable=False, index=True)
    metric_key = db.Column(db.String(80), nullable=False, index=True)
    scope_type = db.Column(db.String(40), nullable=False, default="firm")
    scope_id = db.Column(db.Integer, nullable=True)
    value_num = db.Column(db.Float, nullable=True)
    value_text = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.Index("ix_analytics_metric_snapshot_scope_key", "as_of_date", "scope_type", "scope_id", "metric_key"),
    )


class WorkloadForecast(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    as_of_date = db.Column(db.Date, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    predicted_hours = db.Column(db.Float, nullable=False)
    confidence = db.Column(db.Float, nullable=True)
    features_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class BurnoutSignal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    as_of_date = db.Column(db.Date, nullable=False, index=True)
    score = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(40), nullable=False, default="open")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# AI Platform Foundations (Phase 0 / Phase 1)
# ---------------------------------------------------------------------------


class AIOperationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operation_type = db.Column(db.String(80), nullable=False, index=True)
    provider = db.Column(db.String(40), nullable=False, default="openai")
    model = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="ok", index=True)
    request_chars = db.Column(db.Integer, nullable=False, default=0)
    response_units = db.Column(db.Integer, nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    redaction_applied = db.Column(db.Boolean, nullable=False, default=False)
    metadata_json = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now, index=True)


class SemanticIndexEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_type = db.Column(db.String(40), nullable=False, index=True)
    source_id = db.Column(db.Integer, nullable=False, index=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=True, index=True)
    chunk_index = db.Column(db.Integer, nullable=False, default=0)
    title = db.Column(db.String(255), nullable=True)
    content_text = db.Column(db.Text, nullable=False)
    content_sha256 = db.Column(db.String(64), nullable=False, index=True)
    embedding_json = db.Column(db.Text, nullable=False)
    embedding_model = db.Column(db.String(120), nullable=True)
    embedding_dim = db.Column(db.Integer, nullable=False, default=0)
    provider = db.Column(db.String(40), nullable=True)
    redaction_meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("source_type", "source_id", "chunk_index", name="uq_semantic_index_source_chunk"),
        db.Index("ix_semantic_index_matter_source", "matter_id", "source_type", "source_id"),
        db.Index("ix_semantic_index_source_updated", "source_type", "source_id", "updated_at"),
    )


# ---------------------------------------------------------------------------
# Job Queue and Operations
# ---------------------------------------------------------------------------


class JobQueue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_type = db.Column(db.String(80), nullable=False, index=True)
    payload_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)
    worker_id = db.Column(db.String(80), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=5)
    last_error = db.Column(db.Text, nullable=True)
    run_after = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (
        db.Index("ix_job_queue_claim", "status", "run_after", "lease_until", "created_at"),
    )


class JobHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job_queue.id"), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class ScheduledJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_type = db.Column(db.String(80), nullable=False, index=True)
    default_payload = db.Column(db.JSON, nullable=True)
    interval_minutes = db.Column(db.Integer, nullable=False, default=60)
    next_run_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_run_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (db.Index("ix_scheduled_job_active_next_run", "is_active", "next_run_at"),)


class BackupRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    finished_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="running")
    location = db.Column(db.String(255), nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    triggered_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class RestoreVerification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    backup_run_id = db.Column(db.Integer, db.ForeignKey("backup_run.id"), nullable=True)
    verified_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    status = db.Column(db.String(40), nullable=False, default="passed")
    notes = db.Column(db.Text, nullable=True)
    verified_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class DRTarget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    rpo_minutes_target = db.Column(db.Integer, nullable=False)
    rto_minutes_target = db.Column(db.Integer, nullable=False)
    last_actual_rpo_minutes = db.Column(db.Integer, nullable=True)
    last_actual_rto_minutes = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now)
