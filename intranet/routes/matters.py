from __future__ import annotations

import datetime as dt
import os
import uuid

from flask import abort, flash, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..config import ALLOWED_DOC_EXT, MATTER_STATUSES, is_valid_email
from ..extensions import db
from ..helpers import (
    allowed_doc,
    audit,
    can_access_matter,
    is_admin,
    normalize_query,
    sha256_file,
)
from ..models import DocumentFile, Matter, MatterMember, Task, User
from ..templates import page


def register_matter_routes(app):
    @app.get("/matters")
    @login_required
    def matters():
        q = normalize_query(request.args.get("q", ""))
        base = Matter.query
        if not is_admin():
            base = (
                base.join(MatterMember, MatterMember.matter_id == Matter.id)
                .filter(MatterMember.user_id == current_user.id)
            )
        if q:
            like = f"%{q}%"
            base = base.filter((Matter.matter_no.ilike(like)) | (Matter.title.ilike(like)) | (Matter.client_name.ilike(like)))
        ms = base.order_by(Matter.opened_at.desc()).limit(200).all()
        return page("Matters", "matters/list.html", ms=ms, q=q)

    @app.route("/matters/new", methods=["GET", "POST"])
    @login_required
    def matter_create():
        if request.method == "POST":
            matter_no = normalize_query(request.form.get("matter_no", "")).upper()
            title = normalize_query(request.form.get("title", ""))
            client_name = normalize_query(request.form.get("client_name", ""))
            status = normalize_query(request.form.get("status", "Open")) or "Open"
            description = (request.form.get("description") or "").strip()

            if not matter_no or not title or not client_name:
                flash("Matter number, title, and client name are required.", "warning")
                return redirect(url_for("matter_create"))

            if Matter.query.filter_by(matter_no=matter_no).first():
                flash("Matter number already exists.", "warning")
                return redirect(url_for("matter_create"))
            if status not in MATTER_STATUSES:
                flash("Invalid matter status.", "warning")
                return redirect(url_for("matter_create"))

            m = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status=status,
                description=description,
                created_by=current_user.id,
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(MatterMember(matter_id=m.id, user_id=current_user.id, role_in_matter="Responsible"))
            db.session.commit()

            audit("matter_create", "Matter", m.id, {"matter_no": m.matter_no})
            flash("Matter created.", "info")
            return redirect(url_for("matter_detail", matter_id=m.id))

        return page("New Matter", "matters/new.html")

    @app.get("/matters/<int:matter_id>")
    @login_required
    def matter_detail(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)

        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(50).all()
        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(30).all()

        return page(f"Matter {m.matter_no}", "matters/detail.html", m=m, members=members, tasks=tasks, docs=docs)

    @app.route("/matters/<int:matter_id>/team", methods=["GET", "POST"])
    @login_required
    def matter_team(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            email = normalize_query(request.form.get("email", "")).lower()
            role_in_matter = normalize_query(request.form.get("role_in_matter", "")) or "Team"
            if not is_valid_email(email):
                flash("Provide a valid email address.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            u = User.query.filter_by(email=email).first()
            if not u:
                flash("No such user. Admin must create them first.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            if MatterMember.query.filter_by(matter_id=matter_id, user_id=u.id).first():
                flash("User already in team.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            db.session.add(MatterMember(matter_id=matter_id, user_id=u.id, role_in_matter=role_in_matter))
            db.session.commit()
            audit("matter_team_add", "Matter", matter_id, {"user_id": u.id, "role_in_matter": role_in_matter})
            flash("Added to matter team.", "info")
            return redirect(url_for("matter_team", matter_id=matter_id))

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        users = User.query.order_by(User.full_name.asc()).limit(500).all()

        return page("Matter Team", "matters/team.html", m=m, members=members, users=users)

    @app.route("/matters/<int:matter_id>/tasks", methods=["GET", "POST"])
    @login_required
    def matter_tasks(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            title = normalize_query(request.form.get("title", ""))
            description = (request.form.get("description") or "").strip()
            due = normalize_query(request.form.get("due_date", ""))
            assigned_to_email = normalize_query(request.form.get("assigned_to", "")).lower()

            if not title:
                flash("Task title is required.", "warning")
                return redirect(url_for("matter_tasks", matter_id=matter_id))

            due_date = None
            if due:
                try:
                    due_date = dt.date.fromisoformat(due)
                except ValueError:
                    flash("Invalid due date. Use YYYY-MM-DD.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))

            assigned_to = None
            if assigned_to_email:
                u = User.query.filter_by(email=assigned_to_email).first()
                if not u:
                    flash("Assigned-to user not found.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))
                assigned_to = u.id

            t = Task(
                matter_id=matter_id,
                title=title,
                description=description,
                due_date=due_date,
                assigned_to=assigned_to,
                created_by=current_user.id,
            )
            db.session.add(t)
            db.session.commit()
            audit("task_create", "Task", t.id, {"matter_id": matter_id})
            flash("Task created.", "info")
            return redirect(url_for("matter_tasks", matter_id=matter_id))

        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(200).all()
        users = User.query.order_by(User.full_name.asc()).limit(500).all()
        users_map = {u.id: u for u in users}

        return page("Matter Tasks", "matters/tasks.html", m=m, tasks=tasks, users_map=users_map, users=users)

    @app.post("/tasks/<int:task_id>/status")
    @login_required
    def task_update(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        status = normalize_query(request.form.get("status", "Todo")) or "Todo"
        if status not in {"Todo", "Doing", "Done"}:
            abort(400)
        t.status = status
        db.session.commit()
        audit("task_status", "Task", t.id, {"status": status})
        return redirect(url_for("matter_tasks", matter_id=t.matter_id))

    @app.route("/matters/<int:matter_id>/documents", methods=["GET", "POST"])
    @login_required
    def matter_documents(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            if "file" not in request.files:
                flash("No file uploaded.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            f = request.files["file"]
            if not f or not f.filename:
                flash("No file selected.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            if not allowed_doc(f.filename):
                flash("File type not allowed.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))

            safe = secure_filename(f.filename)
            if not safe:
                flash("Invalid filename.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            stored = f"{matter_id}_{uuid.uuid4().hex}_{safe}"
            dest = os.path.join(app.config["UPLOAD_DIR"], stored)
            f.save(dest)

            doc = DocumentFile(
                matter_id=matter_id,
                original_filename=safe,
                stored_filename=stored,
                sha256=sha256_file(dest),
                content_type=f.mimetype,
                uploaded_by=current_user.id,
            )
            db.session.add(doc)
            db.session.commit()
            audit("document_upload", "DocumentFile", doc.id, {"matter_id": matter_id, "filename": safe})
            flash("Uploaded.", "info")
            return redirect(url_for("matter_documents", matter_id=matter_id))

        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(200).all()

        return page("Documents", "matters/documents.html", m=m, docs=docs, allowed=sorted(ALLOWED_DOC_EXT))

    @app.get("/documents/<int:doc_id>/download")
    @login_required
    def doc_download(doc_id: int):
        d = db.session.get(DocumentFile, doc_id)
        if not d:
            abort(404)
        if not can_access_matter(d.matter_id):
            abort(403)
        file_path = os.path.join(app.config["UPLOAD_DIR"], d.stored_filename)
        if not os.path.isfile(file_path):
            abort(404)
        audit("document_download", "DocumentFile", d.id, {"matter_id": d.matter_id})
        return send_from_directory(app.config["UPLOAD_DIR"], d.stored_filename, as_attachment=True, download_name=d.original_filename)
