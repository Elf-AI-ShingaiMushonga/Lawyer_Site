from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import json

from flask import abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from ..config import is_valid_email
from ..extensions import db
from ..helpers import audit, is_admin, normalize_query
from ..models import Contact, DocumentFile, JobQueue, KnowledgeBase, Matter, MatterMember, MatterNote, MatterTimelineEvent, Task
from ..policies import visible_matter_ids
from ..roles import role_is_admin
from ..services.matter_magic import build_matter_launch_pack, build_matter_magic_snapshot
from ..services.semantic_search import SemanticSearchService
from ..templates import page


def register_content_routes(app):
    def _search_primary_matter(query: str, matters: list[Matter], matter_by_id: dict[int, Matter], tasks: list[Task], docs: list[DocumentFile]) -> Matter | None:
        normalized = str(query or "").strip().casefold()
        if not normalized:
            return matters[0] if matters else None

        scored: list[tuple[int, Matter]] = []
        seen_ids: set[int] = set()
        linked_task_counts: dict[int, int] = {}
        linked_doc_counts: dict[int, int] = {}
        for row in tasks:
            if row.matter_id:
                linked_task_counts[int(row.matter_id)] = linked_task_counts.get(int(row.matter_id), 0) + 1
        for row in docs:
            if row.matter_id:
                linked_doc_counts[int(row.matter_id)] = linked_doc_counts.get(int(row.matter_id), 0) + 1

        for matter in list(matters) + list(matter_by_id.values()):
            if matter is None or getattr(matter, "id", None) is None:
                continue
            matter_id = int(matter.id)
            if matter_id in seen_ids:
                continue
            seen_ids.add(matter_id)
            score = 0
            matter_no = str(getattr(matter, "matter_no", "") or "").casefold()
            title = str(getattr(matter, "title", "") or "").casefold()
            client_name = str(getattr(matter, "client_name", "") or "").casefold()
            if matter_no == normalized:
                score += 120
            elif normalized in matter_no:
                score += 80
            if title == normalized:
                score += 90
            elif normalized in title:
                score += 55
            if client_name == normalized:
                score += 70
            elif normalized in client_name:
                score += 40
            score += linked_task_counts.get(matter_id, 0) * 12
            score += linked_doc_counts.get(matter_id, 0) * 8
            if score > 0:
                scored.append((score, matter))

        if scored:
            scored.sort(key=lambda row: (-row[0], str(row[1].matter_no or "")))
            return scored[0][1]
        return matters[0] if matters else (next(iter(matter_by_id.values()), None) if matter_by_id else None)

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
            a.updated_at = utc_now()
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
        semantic_hits = []
        matter_by_id: dict[int, Matter] = {}
        launch_pack = None
        query_too_short = bool(q) and len(q) < 2
        scoped_matter_ids: set[int] | None = None
        if q and not query_too_short:
            like = f"%{q}%"
            m_base = Matter.query
            task_base = Task.query
            doc_base = DocumentFile.query
            if not is_admin():
                scoped_matter_ids = visible_matter_ids()
                if not scoped_matter_ids:
                    m_base = m_base.filter(Matter.id == -1)
                    task_base = task_base.filter(Task.id == -1)
                    doc_base = doc_base.filter(DocumentFile.id == -1)
                else:
                    m_base = m_base.filter(Matter.id.in_(scoped_matter_ids))
                    task_base = task_base.filter(Task.matter_id.in_(scoped_matter_ids))
                    doc_base = doc_base.filter(DocumentFile.matter_id.in_(scoped_matter_ids))
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
            matter_by_id = {m.id: m for m in matters}
            extra_matter_ids = {int(row.matter_id) for row in tasks if row.matter_id} | {
                int(row.matter_id) for row in docs if row.matter_id
            }
            extra_matter_ids = {matter_id for matter_id in extra_matter_ids if matter_id not in matter_by_id}
            if extra_matter_ids:
                rows = Matter.query.filter(Matter.id.in_(extra_matter_ids)).all()
                for row in rows:
                    matter_by_id[row.id] = row
            if bool(current_app.config.get("AI_SEMANTIC_SEARCH_ENABLED", False)):
                try:
                    semantic_hits = SemanticSearchService.search(
                        q,
                        matter_scope_ids=scoped_matter_ids if not is_admin() else None,
                        limit=8,
                    )
                except Exception as exc:  # pragma: no cover - resilience guard for provider/database failures
                    current_app.logger.warning("Semantic search unavailable: %s", exc)
                    semantic_hits = []

            primary_matter = _search_primary_matter(q, matters, matter_by_id, tasks, docs)
            if primary_matter is not None:
                matter_id = int(primary_matter.id)
                launch_tasks = [row for row in tasks if int(getattr(row, "matter_id", 0) or 0) == matter_id][:8]
                launch_docs = [row for row in docs if int(getattr(row, "matter_id", 0) or 0) == matter_id][:8]
                launch_timeline = (
                    MatterTimelineEvent.query.filter(
                        MatterTimelineEvent.matter_id == matter_id,
                        MatterTimelineEvent.event_date >= dt.date.today(),
                    )
                    .order_by(MatterTimelineEvent.event_date.asc(), MatterTimelineEvent.id.asc())
                    .limit(8)
                    .all()
                )
                snapshot = build_matter_magic_snapshot(
                    primary_matter,
                    today=dt.date.today(),
                    tasks=launch_tasks,
                    docs=launch_docs,
                    timeline=launch_timeline,
                    team_size=MatterMember.query.filter_by(matter_id=matter_id).count(),
                    notes_count=MatterNote.query.filter_by(matter_id=matter_id).count(),
                    limit_actions=4,
                ) or {}
                launch_pack = build_matter_launch_pack(primary_matter, snapshot=snapshot, today=dt.date.today()) or {}

        return page(
            "Search",
            "content/search.html",
            q=q,
            matters=matters,
            tasks=tasks,
            docs=docs,
            articles=articles,
            contacts=contacts,
            semantic_hits=semantic_hits,
            matter_by_id=matter_by_id,
            query_too_short=query_too_short,
            launch_pack=launch_pack,
        )

    @app.get("/api/ai/jobs/<int:job_id>")
    @login_required
    def ai_job_status(job_id: int):
        row = db.session.get(JobQueue, job_id)
        if row is None or not str(row.job_type or "").startswith("semantic_"):
            abort(404)

        requested_by = None
        payload = {}
        if row.payload_json:
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                payload = {}
        if isinstance(payload, dict):
            raw_requested_by = payload.get("requested_by")
            if raw_requested_by is not None:
                try:
                    requested_by = int(raw_requested_by)
                except (TypeError, ValueError):
                    requested_by = None

        if not role_is_admin(getattr(current_user, "role", None)) and requested_by and int(current_user.id) != requested_by:
            abort(403)

        return jsonify(
            {
                "ok": True,
                "job": {
                    "id": int(row.id),
                    "job_type": row.job_type,
                    "status": row.status,
                    "attempts": int(row.attempts or 0),
                    "max_attempts": int(row.max_attempts or 0),
                    "worker_id": row.worker_id,
                    "last_error": row.last_error,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                },
            }
        )
