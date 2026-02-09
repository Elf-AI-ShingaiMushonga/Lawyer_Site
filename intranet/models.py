from __future__ import annotations

import datetime as dt

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
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
    role = db.Column(db.String(40), nullable=False, default="lawyer")
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def get_id(self):
        return str(self.id)


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Matter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_no = db.Column(db.String(80), unique=True, nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    client_name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(40), nullable=False, default="Open")
    description = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class MatterMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role_in_matter = db.Column(db.String(80), nullable=False, default="Team")
    __table_args__ = (db.UniqueConstraint("matter_id", "user_id", name="uq_matter_user"),)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="Todo")
    due_date = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)


class DocumentFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Integer, db.ForeignKey("matter.id"), nullable=False, index=True)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    content_type = db.Column(db.String(120), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)


class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    organization = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class KnowledgeBase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    tags = db.Column(db.String(255), nullable=True)
    body = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)
