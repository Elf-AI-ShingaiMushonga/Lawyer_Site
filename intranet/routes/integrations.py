from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from xml.sax.saxutils import escape as xml_escape

from flask import Response, abort, flash, redirect, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from ..extensions import db
from ..helpers import audit, can_access_matter, is_admin
from ..models import (
    Deadline,
    ExpenseEntry,
    FirmSetting,
    Invoice,
    Matter,
    MatterMember,
    Task,
    TaskAssignee,
    TimeEntry,
    TimeRoundingPolicy,
    User,
)
from ..policies import visible_matter_ids
from ..templates import page


def _parse_date(value: str | None, fallback: dt.date) -> dt.date:
    raw = (value or "").strip()
    if not raw:
        return fallback
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return fallback


def _parse_datetime(value: str | None) -> dt.datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _ics_escape(value: str) -> str:
    text = (value or "").replace("\\", "\\\\")
    text = text.replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    return text


def _get_json_setting(setting_key: str, default: dict) -> tuple[FirmSetting | None, dict]:
    row = FirmSetting.query.filter_by(setting_key=setting_key).first()
    if row is None:
        return None, dict(default)
    try:
        parsed = json.loads(row.setting_value_json or "{}")
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    merged = dict(default)
    merged.update(parsed)
    return row, merged


def _save_json_setting(setting_key: str, payload: dict) -> None:
    row = FirmSetting.query.filter_by(setting_key=setting_key).first()
    now = dt.datetime.utcnow()
    if row is None:
        row = FirmSetting(
            setting_key=setting_key,
            setting_value_json=json.dumps(payload),
            updated_at=now,
            updated_by=current_user.id,
        )
        db.session.add(row)
    else:
        row.setting_value_json = json.dumps(payload)
        row.updated_at = now
        row.updated_by = current_user.id
    db.session.commit()


def _scope_matter_ids(scope: str) -> list[int]:
    if scope == "team" and is_admin():
        return [row.id for row in Matter.query.order_by(Matter.id.asc()).all()]
    return visible_matter_ids()


def _active_rounding_policy(matter: Matter) -> TimeRoundingPolicy | None:
    policy = (
        TimeRoundingPolicy.query.filter_by(matter_id=matter.id, is_active=True)
        .order_by(TimeRoundingPolicy.id.desc())
        .first()
    )
    if policy is not None:
        return policy
    return (
        TimeRoundingPolicy.query.filter_by(client_name=matter.client_name, is_active=True)
        .order_by(TimeRoundingPolicy.id.desc())
        .first()
    )


def _round_hours(hours: float, increment: float) -> float:
    if increment <= 0:
        return round(hours, 4)
    steps = round(hours / increment)
    return round(steps * increment, 4)


def _make_simple_docx(text: str) -> bytes:
    lines = text.splitlines() or [text]
    paragraphs: list[str] = []
    for line in lines:
        escaped = xml_escape(line)
        paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>')
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + (
            "<w:sectPr>"
            '<w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
            "</w:sectPr>"
            "</w:body></w:document>"
        )
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("[Content_Types].xml", content_types)
        bundle.writestr("_rels/.rels", rels)
        bundle.writestr("word/document.xml", document_xml)
    out.seek(0)
    return out.read()


def _load_csv_rows(upload) -> list[dict]:
    raw = upload.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader if isinstance(row, dict)]


def register_integration_routes(app):
    @app.route("/integrations/office365", methods=["GET", "POST"])
    @login_required
    def integrations_office365():
        default_config = {
            "enabled": False,
            "tenant_id": "",
            "client_id": "",
            "domain_hint": "",
            "sync_notes": "",
        }
        _, config = _get_json_setting("office365_integration", default_config)
        if request.method == "POST":
            if not is_admin():
                abort(403)
            payload = {
                "enabled": (request.form.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"},
                "tenant_id": (request.form.get("tenant_id") or "").strip(),
                "client_id": (request.form.get("client_id") or "").strip(),
                "domain_hint": (request.form.get("domain_hint") or "").strip(),
                "sync_notes": (request.form.get("sync_notes") or "").strip(),
            }
            _save_json_setting("office365_integration", payload)
            audit("office365_settings_save", "FirmSetting", None, {"enabled": payload["enabled"]})
            flash("Office365 integration settings saved.", "info")
            return redirect(url_for("integrations_office365"))

        today = dt.date.today()
        stats = {
            "deadlines_next_30d": Deadline.query.filter(
                Deadline.due_at >= today,
                Deadline.due_at <= today + dt.timedelta(days=30),
            ).count(),
            "time_entries_week": TimeEntry.query.filter(
                TimeEntry.start_at >= dt.datetime.combine(today - dt.timedelta(days=7), dt.time.min)
            ).count(),
            "invoices_month": Invoice.query.filter(
                Invoice.created_at >= dt.datetime.combine(today.replace(day=1), dt.time.min)
            ).count(),
        }
        return page(
            "Office365 Integration",
            "integrations/office365.html",
            config=config,
            stats=stats,
            matters=(
                Matter.query.filter(Matter.id.in_(visible_matter_ids() or [-1]))
                .order_by(Matter.last_updated_at.desc())
                .limit(250)
                .all()
                if not is_admin()
                else Matter.query.order_by(Matter.last_updated_at.desc()).limit(250).all()
            ),
        )

    @app.get("/integrations/office365/outlook.ics")
    @login_required
    def integrations_office365_outlook_feed():
        today = dt.date.today()
        start = _parse_date(request.args.get("start"), today - dt.timedelta(days=7))
        end = _parse_date(request.args.get("end"), today + dt.timedelta(days=90))
        if end < start:
            end = start
        scope = (request.args.get("scope") or "my").strip().lower()
        matter_ids = _scope_matter_ids(scope)
        if not matter_ids:
            matter_ids = [-1]

        rows = (
            db.session.query(Deadline, Matter)
            .join(Matter, Matter.id == Deadline.matter_id)
            .filter(
                Deadline.matter_id.in_(matter_ids),
                Deadline.due_at >= start,
                Deadline.due_at <= end,
            )
            .order_by(Deadline.due_at.asc(), Deadline.id.asc())
            .all()
        )
        now_stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//DM-Inc Intranet//Office365//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:DM-Inc Intranet Deadlines",
        ]
        for deadline, matter in rows:
            start_value = deadline.due_at.strftime("%Y%m%d")
            end_value = (deadline.due_at + dt.timedelta(days=1)).strftime("%Y%m%d")
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:deadline-{deadline.id}@dm-inc-intranet.local",
                    f"DTSTAMP:{now_stamp}",
                    f"DTSTART;VALUE=DATE:{start_value}",
                    f"DTEND;VALUE=DATE:{end_value}",
                    f"SUMMARY:{_ics_escape(f'{deadline.title} ({matter.matter_no})')}",
                    "DESCRIPTION:"
                    + _ics_escape(
                        f"Matter {matter.matter_no} - {matter.title}\n"
                        f"Client: {matter.client_name}\n"
                        f"Status: {deadline.status}\n"
                        f"Critical: {'Yes' if deadline.is_critical else 'No'}"
                    ),
                    "CATEGORIES:Legal Deadline",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")
        audit("office365_outlook_feed_export", "Deadline", None, {"count": len(rows), "scope": scope})
        return Response(
            "\r\n".join(lines) + "\r\n",
            mimetype="text/calendar",
            headers={"Content-Disposition": 'attachment; filename="law-intranet-deadlines.ics"'},
        )

    @app.get("/integrations/office365/excel/time-entries.csv")
    @login_required
    def integrations_office365_excel_time_entries():
        scope = (request.args.get("scope") or "my").strip().lower()
        matter_ids = _scope_matter_ids(scope)
        if not matter_ids:
            matter_ids = [-1]
        period_days = max(1, min(request.args.get("days", type=int) or 60, 365))
        start_at = dt.datetime.combine(dt.date.today() - dt.timedelta(days=period_days), dt.time.min)

        rows = (
            db.session.query(TimeEntry, Matter, User)
            .join(Matter, Matter.id == TimeEntry.matter_id)
            .join(User, User.id == TimeEntry.user_id)
            .filter(TimeEntry.matter_id.in_(matter_ids), TimeEntry.start_at >= start_at)
            .order_by(TimeEntry.start_at.desc(), TimeEntry.id.desc())
            .limit(5000)
            .all()
        )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "entry_id",
                "matter_no",
                "matter_title",
                "client_name",
                "timekeeper",
                "start_at",
                "end_at",
                "hours",
                "rounded_hours",
                "task_code",
                "activity_code",
                "status",
                "is_billable",
            ]
        )
        for entry, matter, user in rows:
            writer.writerow(
                [
                    entry.id,
                    matter.matter_no,
                    matter.title,
                    matter.client_name,
                    user.full_name,
                    entry.start_at.isoformat() if entry.start_at else "",
                    entry.end_at.isoformat() if entry.end_at else "",
                    entry.hours,
                    entry.rounded_hours,
                    entry.task_code or "",
                    entry.activity_code or "",
                    entry.status,
                    "yes" if entry.is_billable else "no",
                ]
            )
        audit("office365_excel_time_export", "TimeEntry", None, {"count": len(rows), "scope": scope})
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="office365-time-entries.csv"'},
        )

    @app.get("/integrations/office365/excel/invoices.csv")
    @login_required
    def integrations_office365_excel_invoices():
        scope = (request.args.get("scope") or "my").strip().lower()
        matter_ids = _scope_matter_ids(scope)
        if not matter_ids:
            matter_ids = [-1]
        rows = (
            db.session.query(Invoice, Matter)
            .join(Matter, Matter.id == Invoice.matter_id)
            .filter(Invoice.matter_id.in_(matter_ids))
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .limit(5000)
            .all()
        )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "invoice_id",
                "matter_no",
                "matter_title",
                "client_name",
                "period_start",
                "period_end",
                "status",
                "subtotal",
                "tax_total",
                "total",
                "created_at",
            ]
        )
        for inv, matter in rows:
            writer.writerow(
                [
                    inv.id,
                    matter.matter_no,
                    matter.title,
                    inv.client_name,
                    inv.period_start.isoformat() if inv.period_start else "",
                    inv.period_end.isoformat() if inv.period_end else "",
                    inv.status,
                    inv.subtotal,
                    inv.tax_total,
                    inv.total,
                    inv.created_at.isoformat() if inv.created_at else "",
                ]
            )
        audit("office365_excel_invoice_export", "Invoice", None, {"count": len(rows), "scope": scope})
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="office365-invoices.csv"'},
        )

    @app.get("/integrations/office365/word/matters/<int:matter_id>/summary.docx")
    @login_required
    def integrations_office365_word_matter_summary(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        matter = db.session.get(Matter, matter_id)
        if matter is None:
            abort(404)
        text = "\n".join(
            [
                f"Matter Summary: {matter.matter_no}",
                f"Title: {matter.title}",
                f"Client: {matter.client_name}",
                f"Status: {matter.status}",
                f"Stage: {matter.stage or '-'}",
                f"Jurisdiction: {matter.jurisdiction or '-'}",
                f"Practice Area: {matter.practice_area or '-'}",
                f"Case Type: {matter.case_type or '-'}",
                "",
                "Description:",
                matter.description or "-",
                "",
                "Last Update:",
                matter.last_update_note or "-",
                "",
                "Outcome Summary:",
                matter.outcome_summary or "-",
            ]
        )
        payload = _make_simple_docx(text)
        filename = f"{matter.matter_no.lower().replace('/', '-')}_summary.docx"
        audit("office365_word_matter_export", "Matter", matter.id)
        return send_file(
            io.BytesIO(payload),
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    @app.get("/integrations/office365/word/matter-summary.docx")
    @login_required
    def integrations_office365_word_matter_summary_query():
        matter_id = request.args.get("matter_id", type=int)
        if not matter_id:
            flash("Matter selection is required for Word export.", "warning")
            return redirect(url_for("integrations_office365"))
        return integrations_office365_word_matter_summary(matter_id)

    @app.route("/integrations/third-party", methods=["GET", "POST"])
    @login_required
    def integrations_third_party():
        if request.method == "POST":
            action = (request.form.get("action") or "").strip().lower()
            if action == "import_cost_recovery":
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    flash("CSV file is required for cost recovery import.", "warning")
                    return redirect(url_for("integrations_third_party"))
                rows = _load_csv_rows(upload)
                created = 0
                skipped = 0
                errors = 0
                for row in rows:
                    matter_no = (row.get("matter_no") or "").strip().upper()
                    if not matter_no:
                        errors += 1
                        continue
                    matter = Matter.query.filter_by(matter_no=matter_no).first()
                    if matter is None or not can_access_matter(matter.id):
                        skipped += 1
                        continue

                    start_at = _parse_datetime(row.get("start_at"))
                    end_at = _parse_datetime(row.get("end_at"))
                    if start_at is None:
                        errors += 1
                        continue
                    if end_at is None:
                        hours_raw = (row.get("hours") or "").strip()
                        try:
                            hours = float(hours_raw)
                        except (TypeError, ValueError):
                            errors += 1
                            continue
                        if hours <= 0:
                            errors += 1
                            continue
                        end_at = start_at + dt.timedelta(hours=hours)
                    if end_at <= start_at:
                        errors += 1
                        continue

                    narrative = (row.get("narrative") or "").strip() or None
                    duplicate = (
                        TimeEntry.query.filter(
                            TimeEntry.user_id == current_user.id,
                            TimeEntry.matter_id == matter.id,
                            TimeEntry.start_at == start_at,
                            TimeEntry.end_at == end_at,
                            TimeEntry.narrative == narrative,
                        )
                        .limit(1)
                        .first()
                    )
                    if duplicate is not None:
                        skipped += 1
                        continue

                    policy = _active_rounding_policy(matter)
                    raw_hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
                    rounded = _round_hours(raw_hours, float(policy.increment_hours if policy else 0.1))
                    entry = TimeEntry(
                        user_id=current_user.id,
                        matter_id=matter.id,
                        start_at=start_at,
                        end_at=end_at,
                        hours=round(raw_hours, 4),
                        rounded_hours=rounded,
                        narrative=narrative,
                        task_code=(row.get("task_code") or "").strip() or None,
                        activity_code=(row.get("activity_code") or "").strip() or None,
                        is_billable=((row.get("is_billable") or "1").strip().lower() in {"1", "true", "yes", "on"}),
                        status="draft",
                    )
                    db.session.add(entry)
                    created += 1
                db.session.commit()
                audit(
                    "third_party_import_cost_recovery",
                    "TimeEntry",
                    None,
                    {"created": created, "skipped": skipped, "errors": errors},
                )
                flash(
                    f"Cost recovery import complete. Created: {created}, skipped: {skipped}, errors: {errors}.",
                    "info",
                )
                return redirect(url_for("integrations_third_party"))

            if action == "import_conveyancing":
                if not is_admin():
                    abort(403)
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    flash("CSV file is required for conveyancing import.", "warning")
                    return redirect(url_for("integrations_third_party"))
                rows = _load_csv_rows(upload)
                created = 0
                updated = 0
                errors = 0
                for row in rows:
                    matter_no = (row.get("matter_no") or "").strip().upper()
                    title = (row.get("title") or "").strip()
                    client_name = (row.get("client_name") or "").strip()
                    if not matter_no or not title or not client_name:
                        errors += 1
                        continue

                    matter = Matter.query.filter_by(matter_no=matter_no).first()
                    if matter is None:
                        matter = Matter(
                            matter_no=matter_no,
                            title=title,
                            client_name=client_name,
                            status=(row.get("status") or "Open").strip() or "Open",
                            created_by=current_user.id,
                            opened_at=dt.datetime.utcnow(),
                            last_updated_at=dt.datetime.utcnow(),
                        )
                        db.session.add(matter)
                        db.session.flush()
                        db.session.add(
                            MatterMember(
                                matter_id=matter.id,
                                user_id=current_user.id,
                                role_in_matter="Responsible",
                            )
                        )
                        created += 1
                    else:
                        matter.title = title
                        matter.client_name = client_name
                        matter.status = (row.get("status") or matter.status).strip() or matter.status
                        matter.last_updated_at = dt.datetime.utcnow()
                        updated += 1

                    matter.practice_area = (row.get("practice_area") or matter.practice_area or "Conveyancing").strip()
                    matter.case_type = (row.get("case_type") or matter.case_type or "Property Transfer").strip()
                    matter.jurisdiction = (row.get("jurisdiction") or matter.jurisdiction or "").strip() or None
                    matter.stage = (row.get("stage") or matter.stage or "").strip() or None
                db.session.commit()
                audit(
                    "third_party_import_conveyancing",
                    "Matter",
                    None,
                    {"created": created, "updated": updated, "errors": errors},
                )
                flash(
                    f"Conveyancing import complete. Created: {created}, updated: {updated}, errors: {errors}.",
                    "info",
                )
                return redirect(url_for("integrations_third_party"))

            flash("Unknown third-party action.", "warning")
            return redirect(url_for("integrations_third_party"))

        matter_scope_ids = visible_matter_ids() if not is_admin() else [row.id for row in Matter.query.with_entities(Matter.id).all()]
        scoped_matter_count = len(matter_scope_ids)
        pending_export_rows = (
            TimeEntry.query.filter(
                TimeEntry.matter_id.in_(matter_scope_ids or [-1]),
                TimeEntry.status.in_(["approved", "draft", "needs_review"]),
            ).count()
            if matter_scope_ids
            else 0
        )
        return page(
            "Third-Party Integrations",
            "integrations/third_party.html",
            scoped_matter_count=scoped_matter_count,
            pending_export_rows=pending_export_rows,
        )

    @app.get("/integrations/third-party/export/cost-recovery.csv")
    @login_required
    def integrations_export_cost_recovery():
        matter_ids = visible_matter_ids() if not is_admin() else [row.id for row in Matter.query.with_entities(Matter.id).all()]
        if not matter_ids:
            matter_ids = [-1]
        start = _parse_date(request.args.get("start"), dt.date.today() - dt.timedelta(days=60))
        end = _parse_date(request.args.get("end"), dt.date.today())
        if end < start:
            end = start
        start_dt = dt.datetime.combine(start, dt.time.min)
        end_dt = dt.datetime.combine(end, dt.time.max)

        time_rows = (
            db.session.query(TimeEntry, Matter)
            .join(Matter, Matter.id == TimeEntry.matter_id)
            .filter(
                TimeEntry.matter_id.in_(matter_ids),
                TimeEntry.start_at >= start_dt,
                TimeEntry.start_at <= end_dt,
            )
            .order_by(TimeEntry.start_at.asc(), TimeEntry.id.asc())
            .all()
        )
        expense_rows = (
            db.session.query(ExpenseEntry, Matter)
            .join(Matter, Matter.id == ExpenseEntry.matter_id)
            .filter(
                ExpenseEntry.matter_id.in_(matter_ids),
                ExpenseEntry.incurred_on >= start,
                ExpenseEntry.incurred_on <= end,
            )
            .order_by(ExpenseEntry.incurred_on.asc(), ExpenseEntry.id.asc())
            .all()
        )

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "record_type",
                "source_id",
                "matter_no",
                "matter_title",
                "client_name",
                "date",
                "hours",
                "amount",
                "currency",
                "task_code",
                "activity_code",
                "description",
                "status",
            ]
        )
        for entry, matter in time_rows:
            writer.writerow(
                [
                    "time_entry",
                    entry.id,
                    matter.matter_no,
                    matter.title,
                    matter.client_name,
                    entry.start_at.date().isoformat() if entry.start_at else "",
                    entry.rounded_hours,
                    "",
                    "",
                    entry.task_code or "",
                    entry.activity_code or "",
                    entry.narrative or "",
                    entry.status,
                ]
            )
        for expense, matter in expense_rows:
            writer.writerow(
                [
                    "expense",
                    expense.id,
                    matter.matter_no,
                    matter.title,
                    matter.client_name,
                    expense.incurred_on.isoformat() if expense.incurred_on else "",
                    "",
                    expense.amount,
                    expense.currency,
                    "",
                    "",
                    expense.description or expense.category or "",
                    expense.status,
                ]
            )
        audit(
            "third_party_export_cost_recovery",
            "TimeEntry",
            None,
            {"time_rows": len(time_rows), "expense_rows": len(expense_rows)},
        )
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="cost-recovery-export.csv"'},
        )

    @app.get("/integrations/third-party/export/conveyancing.csv")
    @login_required
    def integrations_export_conveyancing():
        matter_query = Matter.query.filter(
            or_(
                Matter.practice_area.ilike("%convey%"),
                Matter.case_type.ilike("%transfer%"),
                Matter.case_type.ilike("%property%"),
            )
        )
        if not is_admin():
            scope_ids = visible_matter_ids()
            if scope_ids:
                matter_query = matter_query.filter(Matter.id.in_(scope_ids))
            else:
                matter_query = matter_query.filter(Matter.id == -1)
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(5000).all()

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "matter_no",
                "title",
                "client_name",
                "status",
                "practice_area",
                "case_type",
                "stage",
                "jurisdiction",
                "opened_at",
                "last_updated_at",
            ]
        )
        for matter in matters:
            writer.writerow(
                [
                    matter.matter_no,
                    matter.title,
                    matter.client_name,
                    matter.status,
                    matter.practice_area or "",
                    matter.case_type or "",
                    matter.stage or "",
                    matter.jurisdiction or "",
                    matter.opened_at.isoformat() if matter.opened_at else "",
                    matter.last_updated_at.isoformat() if matter.last_updated_at else "",
                ]
            )
        audit("third_party_export_conveyancing", "Matter", None, {"count": len(matters)})
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="conveyancing-export.csv"'},
        )

    @app.post("/integrations/third-party/import/cost-recovery")
    @login_required
    def integrations_import_cost_recovery():
        return integrations_third_party()

    @app.post("/integrations/third-party/import/conveyancing")
    @login_required
    def integrations_import_conveyancing():
        return integrations_third_party()

    @app.route("/mobile/hub", methods=["GET", "POST"])
    @login_required
    def mobile_hub():
        matter_ids = visible_matter_ids() if not is_admin() else [row.id for row in Matter.query.with_entities(Matter.id).all()]
        matters_query = Matter.query.filter(Matter.id.in_(matter_ids or [-1]))
        matters = matters_query.order_by(Matter.last_updated_at.desc()).limit(200).all()
        matter_by_id = {matter.id: matter for matter in matters}

        user_query = User.query
        if not is_admin():
            member_user_ids = (
                db.session.query(MatterMember.user_id)
                .filter(MatterMember.matter_id.in_(matter_ids or [-1]))
                .distinct()
                .all()
            )
            ids = [row[0] for row in member_user_ids]
            if current_user.id not in ids:
                ids.append(current_user.id)
            user_query = user_query.filter(User.id.in_(ids or [-1]))
        users = user_query.order_by(User.full_name.asc()).limit(500).all()

        if request.method == "POST":
            action = (request.form.get("action") or "").strip().lower()
            if action == "capture_fee":
                matter_id = request.form.get("matter_id", type=int)
                matter = matter_by_id.get(matter_id)
                if matter is None or not can_access_matter(matter.id):
                    abort(403)

                start_at = _parse_datetime(request.form.get("start_at"))
                end_at = _parse_datetime(request.form.get("end_at"))
                duration_minutes = request.form.get("duration_minutes", type=int)
                if start_at is None:
                    flash("Start datetime is required.", "warning")
                    return redirect(url_for("mobile_hub"))
                if end_at is None and duration_minutes:
                    end_at = start_at + dt.timedelta(minutes=max(1, duration_minutes))
                if end_at is None:
                    flash("End datetime or duration is required.", "warning")
                    return redirect(url_for("mobile_hub"))
                if end_at <= start_at:
                    flash("End must be after start.", "warning")
                    return redirect(url_for("mobile_hub"))

                hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
                policy = _active_rounding_policy(matter)
                rounded = _round_hours(hours, float(policy.increment_hours if policy else 0.1))
                entry = TimeEntry(
                    user_id=current_user.id,
                    matter_id=matter.id,
                    start_at=start_at,
                    end_at=end_at,
                    hours=round(hours, 4),
                    rounded_hours=rounded,
                    narrative=(request.form.get("narrative") or "").strip() or None,
                    task_code=(request.form.get("task_code") or "").strip() or None,
                    activity_code=(request.form.get("activity_code") or "").strip() or None,
                    is_billable=(request.form.get("is_billable") or "").strip().lower() in {"1", "true", "yes", "on"},
                    status="draft",
                )
                db.session.add(entry)
                db.session.commit()
                audit("mobile_fee_capture", "TimeEntry", entry.id, {"matter_id": matter.id})
                flash("Fee entry captured from mobile hub.", "info")
                return redirect(url_for("mobile_hub"))

            if action == "assign_task":
                matter_id = request.form.get("matter_id", type=int)
                matter = matter_by_id.get(matter_id)
                if matter is None or not can_access_matter(matter.id):
                    abort(403)
                title = (request.form.get("title") or "").strip()
                if not title:
                    flash("Task title is required.", "warning")
                    return redirect(url_for("mobile_hub"))
                due_date = None
                due_raw = (request.form.get("due_date") or "").strip()
                if due_raw:
                    try:
                        due_date = dt.date.fromisoformat(due_raw)
                    except ValueError:
                        flash("Invalid due date.", "warning")
                        return redirect(url_for("mobile_hub"))

                assignee_ids: list[int] = []
                seen: set[int] = set()
                for raw in request.form.getlist("assignee_user_ids"):
                    raw = (raw or "").strip()
                    if not raw:
                        continue
                    try:
                        user_id = int(raw)
                    except ValueError:
                        continue
                    if user_id in seen:
                        continue
                    if db.session.get(User, user_id) is None:
                        continue
                    assignee_ids.append(user_id)
                    seen.add(user_id)

                task = Task(
                    matter_id=matter.id,
                    title=title,
                    description=(request.form.get("description") or "").strip() or None,
                    due_date=due_date,
                    assigned_to=assignee_ids[0] if assignee_ids else None,
                    created_by=current_user.id,
                    priority=(request.form.get("priority") or "Medium").strip() or "Medium",
                )
                db.session.add(task)
                db.session.flush()
                for user_id in assignee_ids:
                    db.session.add(TaskAssignee(task_id=task.id, user_id=user_id, assigned_by=current_user.id))
                db.session.commit()
                audit("mobile_task_assign", "Task", task.id, {"matter_id": matter.id, "assignee_count": len(assignee_ids)})
                flash("Task assigned from mobile hub.", "info")
                return redirect(url_for("mobile_hub"))

            flash("Unsupported mobile action.", "warning")
            return redirect(url_for("mobile_hub"))

        my_recent_entries = (
            TimeEntry.query.filter_by(user_id=current_user.id)
            .order_by(TimeEntry.start_at.desc(), TimeEntry.id.desc())
            .limit(12)
            .all()
        )
        my_recent_tasks = (
            Task.query.filter(Task.matter_id.in_(matter_ids or [-1]), Task.assigned_to == current_user.id)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .limit(12)
            .all()
        )
        return page(
            "Mobile Hub",
            "integrations/mobile_hub.html",
            matters=matters,
            users=users,
            my_recent_entries=my_recent_entries,
            my_recent_tasks=my_recent_tasks,
            now_iso=dt.datetime.utcnow().replace(second=0, microsecond=0).isoformat(timespec="minutes"),
        )
