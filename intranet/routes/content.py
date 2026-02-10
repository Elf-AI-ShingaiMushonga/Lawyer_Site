from __future__ import annotations

import datetime as dt

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..config import is_valid_email
from ..extensions import db
from ..helpers import audit, is_admin, normalize_query
from ..models import Contact, DocumentFile, KnowledgeBase, Matter, MatterMember, Task
from ..templates import page


def register_content_routes(app):
    @app.route("/contacts", methods=["GET", "POST"])
    @login_required
    def contacts():
        if request.method == "POST":
            name = normalize_query(request.form.get("name", ""))
            organization = normalize_query(request.form.get("organization", ""))
            email = normalize_query(request.form.get("email", "")).lower()
            phone = normalize_query(request.form.get("phone", ""))
            notes = (request.form.get("notes") or "").strip()
            if not name:
                flash("Name is required.", "warning")
                return redirect(url_for("contacts"))
            if email and not is_valid_email(email):
                flash("Email format is invalid.", "warning")
                return redirect(url_for("contacts"))
            c = Contact(
                name=name,
                organization=organization or None,
                email=email or None,
                phone=phone or None,
                notes=notes or None,
                created_by=current_user.id,
            )
            db.session.add(c)
            db.session.commit()
            audit("contact_create", "Contact", c.id)
            flash("Contact added.", "info")
            return redirect(url_for("contacts"))

        q = normalize_query(request.args.get("q", ""))
        base = Contact.query
        if q:
            like = f"%{q}%"
            base = base.filter((Contact.name.ilike(like)) | (Contact.organization.ilike(like)) | (Contact.email.ilike(like)))
        cs = base.order_by(Contact.created_at.desc()).limit(200).all()

        return page("Contacts", "content/contacts.html", cs=cs, q=q)

    @app.route("/kb", methods=["GET", "POST"])
    @login_required
    def kb():
        if request.method == "POST":
            title = normalize_query(request.form.get("title", ""))
            tags = normalize_query(request.form.get("tags", ""))
            body_text = (request.form.get("body") or "").strip()
            if not title or not body_text:
                flash("Title and body are required.", "warning")
                return redirect(url_for("kb"))
            a = KnowledgeBase(title=title, tags=tags or None, body=body_text, created_by=current_user.id)
            db.session.add(a)
            db.session.commit()
            audit("kb_create", "KnowledgeBase", a.id)
            flash("Article created.", "info")
            return redirect(url_for("kb_view", kb_id=a.id))

        q = normalize_query(request.args.get("q", ""))
        base = KnowledgeBase.query
        if q:
            like = f"%{q}%"
            base = base.filter((KnowledgeBase.title.ilike(like)) | (KnowledgeBase.tags.ilike(like)) | (KnowledgeBase.body.ilike(like)))
        items = base.order_by(KnowledgeBase.updated_at.desc()).limit(200).all()

        return page("Knowledge", "content/kb.html", items=items, q=q)

    @app.route("/kb/<int:kb_id>", methods=["GET", "POST"])
    @login_required
    def kb_view(kb_id: int):
        a = db.session.get(KnowledgeBase, kb_id)
        if not a:
            abort(404)

        if request.method == "POST":
            title = normalize_query(request.form.get("title", ""))
            tags = normalize_query(request.form.get("tags", ""))
            body_text = (request.form.get("body") or "").strip()
            if not title or not body_text:
                flash("Title and body required.", "warning")
                return redirect(url_for("kb_view", kb_id=kb_id))
            a.title = title
            a.tags = tags or None
            a.body = body_text
            a.updated_at = dt.datetime.utcnow()
            db.session.commit()
            audit("kb_update", "KnowledgeBase", a.id)
            flash("Updated.", "info")
            return redirect(url_for("kb_view", kb_id=kb_id))

        return page(a.title, "content/kb_view.html", a=a)

    @app.get("/search")
    @login_required
    def search():
        q = normalize_query(request.args.get("q", ""))
        matters = tasks = docs = articles = contacts = []
        if q:
            like = f"%{q}%"
            m_base = Matter.query
            task_base = Task.query
            doc_base = DocumentFile.query
            if not is_admin():
                m_base = (
                    m_base.join(MatterMember, MatterMember.matter_id == Matter.id)
                    .filter(MatterMember.user_id == current_user.id)
                )
                allowed_matter_ids = (
                    db.session.query(MatterMember.matter_id)
                    .filter(MatterMember.user_id == current_user.id)
                )
                task_base = task_base.filter(Task.matter_id.in_(allowed_matter_ids))
                doc_base = doc_base.filter(DocumentFile.matter_id.in_(allowed_matter_ids))
            matters = m_base.filter(
                (Matter.matter_no.ilike(like))
                | (Matter.title.ilike(like))
                | (Matter.client_name.ilike(like))
                | (Matter.objective.ilike(like))
                | (Matter.last_update_note.ilike(like))
                | (Matter.outcome_summary.ilike(like))
            ).limit(25).all()
            tasks = task_base.filter(Task.title.ilike(like) | Task.description.ilike(like)).limit(25).all()
            docs = doc_base.filter(
                (DocumentFile.original_filename.ilike(like))
                | (DocumentFile.category.ilike(like))
                | (DocumentFile.owner_name.ilike(like))
                | (DocumentFile.doc_version.ilike(like))
            ).limit(25).all()
            articles = KnowledgeBase.query.filter(KnowledgeBase.title.ilike(like) | KnowledgeBase.body.ilike(like)).limit(25).all()
            contacts = Contact.query.filter(Contact.name.ilike(like) | Contact.organization.ilike(like) | Contact.email.ilike(like)).limit(25).all()

        return page(
            "Search",
            "content/search.html",
            q=q,
            matters=matters,
            tasks=tasks,
            docs=docs,
            articles=articles,
            contacts=contacts,
        )
