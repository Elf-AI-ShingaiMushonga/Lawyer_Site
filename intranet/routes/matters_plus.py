from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections import defaultdict

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import (
    allowed_audio,
    audit,
    can_access_matter,
    has_active_legal_hold,
    matter_activity,
    normalize_query,
    sha256_file,
)
from ..models import (
    Deadline,
    DocumentFile,
    DocumentRecord,
    Entity,
    Matter,
    MatterClosingChecklistItem,
    MatterMember,
    MatterNote,
    MatterNoteACL,
    MatterParty,
    MatterStageHistory,
    MatterTemplate,
    Task,
    TaskAssignee,
    User,
)
from ..services.workflow_automation import auto_pause_running_timers_for_matter
from ..services.notification_engine import NotificationEngine
from ..templates import page


def register_matters_plus_routes(app):
    @app.route("/matters/intake", methods=["GET", "POST"])
    @login_required
    def matters_intake():
        if request.method == "POST":
            matter_no = normalize_query(request.form.get("matter_no", "")).upper()
            title = normalize_query(request.form.get("title", ""))
            client_name = normalize_query(request.form.get("client_name", ""))
            if not matter_no or not title or not client_name:
                flash("Matter number, title, and client are required.", "warning")
                return redirect(url_for("matters_intake"))
            if Matter.query.filter_by(matter_no=matter_no).first():
                flash("Matter number already exists.", "warning")
                return redirect(url_for("matters_intake"))

            template_id = request.form.get("template_id", type=int)
            template = db.session.get(MatterTemplate, template_id) if template_id else None
            now = dt.datetime.utcnow()
            m = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status="Open",
                description=(request.form.get("description") or "").strip() or None,
                objective=(request.form.get("objective") or "").strip() or None,
                risk_level=(request.form.get("risk_level") or (template.default_risk_level if template else "Medium")) or "Medium",
                budget_status=(request.form.get("budget_status") or "On Track") or "On Track",
                jurisdiction=(request.form.get("jurisdiction") or "ZA").strip() or "ZA",
                stage=(request.form.get("stage") or (template.default_stage if template else "Intake")).strip() or "Intake",
                practice_area=(request.form.get("practice_area") or (template.practice_area if template else "")).strip() or None,
                case_type=(request.form.get("case_type") or "General").strip() or "General",
                created_by=current_user.id,
                last_updated_at=now,
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(MatterMember(matter_id=m.id, user_id=current_user.id, role_in_matter="Responsible"))

            if template and template.checklist_json:
                try:
                    checklist_items = json.loads(template.checklist_json)
                except json.JSONDecodeError:
                    checklist_items = []
                for item in checklist_items:
                    if str(item).strip():
                        db.session.add(MatterClosingChecklistItem(matter_id=m.id, item_text=str(item).strip()))

            db.session.add(
                MatterStageHistory(
                    matter_id=m.id,
                    from_stage=None,
                    to_stage=m.stage or "Intake",
                    reason="Matter intake",
                    changed_by=current_user.id,
                )
            )
            db.session.commit()
            audit("matter_intake", "Matter", m.id, {"matter_no": m.matter_no})
            matter_activity(m.id, "Matter intake created", f"Stage {m.stage}")
            flash("Matter intake created.", "info")
            return redirect(url_for("matter_workspace", matter_id=m.id))

        templates = MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()
        return page("Matter Intake", "matters_plus/intake.html", templates=templates)

    @app.get("/matters/<int:matter_id>/workspace")
    @login_required
    def matter_workspace(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        stats = {
            "open_tasks": Task.query.filter(Task.matter_id == matter_id, Task.status != "Done").count(),
            "deadlines": Deadline.query.filter_by(matter_id=matter_id).count(),
            "documents": DocumentRecord.query.filter_by(matter_id=matter_id).count(),
            "notes": MatterNote.query.filter_by(matter_id=matter_id).count(),
        }

        stage_history = (
            MatterStageHistory.query.filter_by(matter_id=matter_id)
            .order_by(MatterStageHistory.changed_at.desc())
            .limit(20)
            .all()
        )
        checklist = MatterClosingChecklistItem.query.filter_by(matter_id=matter_id).order_by(MatterClosingChecklistItem.id.asc()).all()

        return page(
            f"Matter Workspace {m.matter_no}",
            "matters_plus/workspace.html",
            m=m,
            stats=stats,
            stage_history=stage_history,
            checklist=checklist,
        )

    @app.route("/matters/<int:matter_id>/parties", methods=["GET", "POST"])
    @login_required
    def matter_parties(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            entity_name = normalize_query(request.form.get("entity_name", ""))
            party_role = normalize_query(request.form.get("party_role", "Client")) or "Client"
            if not entity_name:
                flash("Entity name is required.", "warning")
                return redirect(url_for("matter_parties", matter_id=matter_id))

            entity = Entity.query.filter_by(name=entity_name).first()
            if entity is None:
                entity = Entity(
                    name=entity_name,
                    entity_type=(request.form.get("entity_type") or "organization").strip() or "organization",
                    email=(request.form.get("email") or "").strip() or None,
                    phone=(request.form.get("phone") or "").strip() or None,
                )
                db.session.add(entity)
                db.session.flush()

            db.session.add(
                MatterParty(
                    matter_id=matter_id,
                    entity_id=entity.id,
                    party_role=party_role,
                    is_primary=(request.form.get("is_primary") or "").lower() in {"1", "true", "yes", "on"},
                )
            )
            db.session.commit()
            audit("matter_party_add", "Matter", matter_id, {"entity_id": entity.id, "role": party_role})
            matter_activity(matter_id, "Party linked", f"{entity.name} ({party_role})")
            flash("Party linked to matter.", "info")
            return redirect(url_for("matter_parties", matter_id=matter_id))

        parties = (
            db.session.query(MatterParty, Entity)
            .join(Entity, Entity.id == MatterParty.entity_id)
            .filter(MatterParty.matter_id == matter_id)
            .order_by(MatterParty.id.desc())
            .all()
        )
        return page("Matter Parties", "matters_plus/parties.html", m=m, parties=parties)

    @app.route("/matters/<int:matter_id>/notes", methods=["GET", "POST"])
    @login_required
    def matter_notes(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            body = (request.form.get("body") or "").strip()
            voice_file = request.files.get("voice_note")
            has_voice_note = bool(voice_file and (voice_file.filename or "").strip())
            if not body and not has_voice_note:
                flash("Add note text or upload a voice note.", "warning")
                return redirect(url_for("matter_notes", matter_id=matter_id))
            if has_voice_note and not allowed_audio(voice_file.filename or ""):
                flash("Voice note must be one of: .m4a, .mp3, .wav, .ogg, .webm.", "warning")
                return redirect(url_for("matter_notes", matter_id=matter_id))
            note = MatterNote(
                matter_id=matter_id,
                body=body or "Voice note captured.",
                tags=(request.form.get("tags") or "").strip() or None,
                privilege_label=(request.form.get("privilege_label") or "").strip() or None,
                created_by=current_user.id,
            )
            db.session.add(note)
            db.session.flush()

            acl_emails = [x.strip().lower() for x in (request.form.get("acl_emails") or "").split(",") if x.strip()]
            for email in acl_emails:
                user = User.query.filter_by(email=email).first()
                if user:
                    db.session.add(MatterNoteACL(note_id=note.id, user_id=user.id, can_read=True, can_edit=False))

            if has_voice_note and voice_file:
                safe_name = secure_filename(voice_file.filename or "")
                if not safe_name:
                    flash("Invalid voice note filename.", "warning")
                    db.session.rollback()
                    return redirect(url_for("matter_notes", matter_id=matter_id))
                ext = safe_name.rsplit(".", 1)[-1].lower()
                stored_name = f"matter{matter_id}_note{note.id}_{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
                os.makedirs(current_app.config["UPLOAD_DIR"], exist_ok=True)
                path = os.path.join(current_app.config["UPLOAD_DIR"], stored_name)
                voice_file.save(path)
                db.session.add(
                    DocumentFile(
                        matter_id=matter_id,
                        original_filename=safe_name,
                        stored_filename=stored_name,
                        sha256=sha256_file(path),
                        content_type=(voice_file.mimetype or "").strip() or None,
                        category="Voice Note",
                        doc_version="v1",
                        lifecycle_stage="Recorded",
                        owner_name=f"note:{note.id}",
                        is_privileged=bool(note.privilege_label),
                        uploaded_by=current_user.id,
                    )
                )

            db.session.commit()
            audit("matter_note_create", "MatterNote", note.id, {"matter_id": matter_id})
            if has_voice_note:
                audit("matter_voice_note_upload", "MatterNote", note.id, {"matter_id": matter_id})
            matter_activity(matter_id, "Matter note added")
            flash("Note added.", "info")
            return redirect(url_for("matter_notes", matter_id=matter_id))

        notes = MatterNote.query.filter_by(matter_id=matter_id).order_by(MatterNote.created_at.desc()).limit(200).all()
        if current_user.role != "admin" and notes:
            note_ids = [note.id for note in notes]
            acl_rows = (
                MatterNoteACL.query.filter(
                    MatterNoteACL.note_id.in_(note_ids),
                    MatterNoteACL.can_read.is_(True),
                )
                .all()
            )
            acl_by_note: dict[int, set[int]] = {}
            for row in acl_rows:
                acl_by_note.setdefault(row.note_id, set()).add(row.user_id)
            notes = [
                note
                for note in notes
                if not acl_by_note.get(note.id)
                or note.created_by == current_user.id
                or current_user.id in acl_by_note.get(note.id, set())
            ]

        voice_notes_by_note_id: dict[int, list[DocumentFile]] = defaultdict(list)
        note_ids = [note.id for note in notes]
        if note_ids:
            owner_tokens = [f"note:{note_id}" for note_id in note_ids]
            voice_rows = (
                DocumentFile.query.filter(
                    DocumentFile.matter_id == matter_id,
                    DocumentFile.category == "Voice Note",
                    DocumentFile.owner_name.in_(owner_tokens),
                )
                .order_by(DocumentFile.uploaded_at.desc())
                .all()
            )
            for row in voice_rows:
                token = (row.owner_name or "").strip().lower()
                if not token.startswith("note:"):
                    continue
                try:
                    note_id = int(token.split(":", 1)[1])
                except ValueError:
                    continue
                voice_notes_by_note_id[note_id].append(row)

        team_user_ids = {current_user.id, m.created_by}
        member_ids = [int(user_id) for (user_id,) in db.session.query(MatterMember.user_id).filter_by(matter_id=matter_id).all()]
        team_user_ids.update(member_ids)
        team_users = User.query.filter(User.id.in_(team_user_ids)).order_by(User.full_name.asc()).all() if team_user_ids else []

        return page(
            "Matter Notes",
            "matters_plus/notes.html",
            m=m,
            notes=notes,
            voice_notes_by_note_id=voice_notes_by_note_id,
            team_users=team_users,
        )

    @app.post("/matters/<int:matter_id>/stage")
    @login_required
    def matter_stage_update(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        next_stage = normalize_query(request.form.get("stage", ""))
        reason = (request.form.get("reason") or "").strip() or None
        if not next_stage:
            flash("Stage is required.", "warning")
            return redirect(url_for("matter_workspace", matter_id=matter_id))

        prev = m.stage
        m.stage = next_stage
        m.last_updated_at = dt.datetime.utcnow()
        db.session.add(
            MatterStageHistory(
                matter_id=m.id,
                from_stage=prev,
                to_stage=next_stage,
                reason=reason,
                changed_by=current_user.id,
            )
        )
        db.session.commit()
        NotificationEngine.enqueue("matter_stage_changed", current_user.id, f"matter:{m.id}:stage:{next_stage}")
        audit("matter_stage_update", "Matter", matter_id, {"from": prev, "to": next_stage})
        matter_activity(m.id, "Matter stage updated", f"{prev or 'None'} -> {next_stage}")
        flash("Matter stage updated.", "info")
        return redirect(url_for("matter_workspace", matter_id=matter_id))

    @app.post("/matters/<int:matter_id>/close")
    @login_required
    def matter_close(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        for raw in request.form.getlist("checklist_done"):
            item = db.session.get(MatterClosingChecklistItem, int(raw))
            if item and item.matter_id == m.id:
                item.is_done = True
                item.done_at = dt.datetime.utcnow()
                item.done_by = current_user.id

        incomplete = MatterClosingChecklistItem.query.filter_by(matter_id=m.id, is_done=False).count()
        if incomplete > 0:
            flash(f"{incomplete} checklist item(s) remain before close.", "warning")
            db.session.commit()
            return redirect(url_for("matter_workspace", matter_id=m.id))

        m.status = "Closed"
        m.closed_at = dt.datetime.utcnow()
        auto_pause_summary = auto_pause_running_timers_for_matter(
            m.id,
            actor_user_id=current_user.id,
            pause_reason="matter_closed",
        )
        open_task_count = Task.query.filter(Task.matter_id == m.id, Task.status != "Done").count()
        closure_followup_task_id = None
        if open_task_count > 0:
            followup_title = f"Post-close task review for {m.matter_no}"
            existing_followup = (
                Task.query.filter_by(matter_id=m.id, title=followup_title)
                .order_by(Task.id.desc())
                .first()
            )
            if existing_followup is not None:
                closure_followup_task_id = int(existing_followup.id)
            else:
                assignee_id = current_user.id
                lead_member = (
                    MatterMember.query.filter_by(matter_id=m.id)
                    .order_by(MatterMember.id.asc())
                    .first()
                )
                if lead_member is not None:
                    assignee_id = int(lead_member.user_id)
                followup_task = Task(
                    matter_id=m.id,
                    title=followup_title,
                    description=(
                        f"Review {open_task_count} open task(s) after matter closure and document final outcomes."
                    ),
                    status="Todo",
                    due_date=dt.date.today() + dt.timedelta(days=1),
                    assigned_to=assignee_id,
                    created_by=current_user.id,
                    priority="High",
                )
                db.session.add(followup_task)
                db.session.flush()
                db.session.add(
                    TaskAssignee(
                        task_id=followup_task.id,
                        user_id=assignee_id,
                        assigned_by=current_user.id,
                    )
                )
                closure_followup_task_id = int(followup_task.id)
        if has_active_legal_hold(m.id):
            m.archival_status = "legal_hold_blocked"
            m.archival_due_at = None
            db.session.commit()
            audit("matter_close_legal_hold_blocked", "Matter", m.id)
            matter_activity(m.id, "Matter closed with legal hold", "Archival blocked by active legal hold")
            if auto_pause_summary.get("paused", 0) > 0:
                flash(
                    (
                        f"Auto-paused {auto_pause_summary.get('paused', 0)} running timer(s) and captured "
                        f"{auto_pause_summary.get('captured_entries', 0)} draft entry(ies)."
                    ),
                    "info",
                )
            if closure_followup_task_id is not None:
                flash(f"Created follow-up task #{closure_followup_task_id} to resolve open work.", "warning")
            flash("Matter closed. Archival blocked because an active legal hold exists.", "warning")
            return redirect(url_for("matter_workspace", matter_id=m.id))

        m.archival_status = "archive_pending"
        m.archival_due_at = dt.datetime.utcnow() + dt.timedelta(days=30)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            if has_active_legal_hold(m.id):
                m = db.session.get(Matter, m.id)
                if m is None:
                    abort(404)
                m.archival_status = "legal_hold_blocked"
                m.archival_due_at = None
                db.session.commit()
                audit("matter_close_legal_hold_blocked", "Matter", m.id)
                matter_activity(m.id, "Matter closed with legal hold", "Archival blocked by active legal hold")
                flash("Matter closed. Archival blocked because an active legal hold exists.", "warning")
                return redirect(url_for("matter_workspace", matter_id=m.id))
            raise
        audit("matter_close", "Matter", m.id)
        matter_activity(m.id, "Matter closed")
        if auto_pause_summary.get("paused", 0) > 0:
            flash(
                (
                    f"Auto-paused {auto_pause_summary.get('paused', 0)} running timer(s) and captured "
                    f"{auto_pause_summary.get('captured_entries', 0)} draft entry(ies)."
                ),
                "info",
            )
        if closure_followup_task_id is not None:
            flash(f"Created follow-up task #{closure_followup_task_id} to resolve open work.", "warning")
        flash("Matter closed and archival workflow started.", "info")
        return redirect(url_for("matter_workspace", matter_id=m.id))
