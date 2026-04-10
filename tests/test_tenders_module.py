from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.models import Matter, MatterMember, TenderChecklistItem, TenderOpportunity, User
from intranet.routes.tenders import SA_TENDER_CHECKLIST


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _seed_user(email: str, *, role: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
        role=role,
        password_hash="x",
        is_active=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_tender(*, created_by: int, bid_manager_user_id: int, status: str = "Sourced") -> TenderOpportunity:
    tender = TenderOpportunity(
        reference_no=f"TDR-{status[:3].upper()}-2026-01",
        title="Appointment of legal services panel",
        issuing_authority="National Treasury",
        province="National",
        tender_type="Tender",
        portal_source="SA eTender Portal",
        status=status,
        closing_at=dt.datetime(2026, 5, 20, 11, 0, 0),
        bid_manager_user_id=bid_manager_user_id,
        created_by=created_by,
    )
    db.session.add(tender)
    db.session.flush()
    for item_key, label in SA_TENDER_CHECKLIST:
        db.session.add(
            TenderChecklistItem(
                tender_id=tender.id,
                item_key=item_key,
                label=label,
                is_required=True,
                status="pending",
                updated_by=created_by,
            )
        )
    db.session.flush()
    return tender


def test_operations_staff_can_create_sa_tender_and_default_checklist(app_ctx):
    app = app_ctx
    staff = _seed_user("tender.ops@example.com", role="operations_staff")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        "/tenders",
        data={
            "csrf_token": "test-csrf",
            "reference_no": "RFP 12/2026",
            "title": "Panel appointment for labour law services",
            "issuing_authority": "Department of Employment and Labour",
            "province": "National",
            "closing_at": "2026-05-20T11:00",
            "briefing_required": "1",
            "bid_manager_user_id": str(staff.id),
        },
        follow_redirects=False,
    )

    tender = TenderOpportunity.query.filter_by(reference_no="RFP 12/2026").first()
    assert response.status_code == 302
    assert tender is not None
    assert f"/tenders/{tender.id}" in (response.headers.get("Location") or "")
    assert tender.status == "Awaiting Briefing"
    assert TenderChecklistItem.query.filter_by(tender_id=tender.id).count() == len(SA_TENDER_CHECKLIST)

    detail_response = client.get(f"/tenders/{tender.id}")
    body = detail_response.get_data(as_text=True)
    assert detail_response.status_code == 200
    assert "South African bid submission checklist" in body
    assert "SBD 4 declaration of interest" in body


def test_operations_staff_can_update_checklist_but_not_convert_to_matter(app_ctx):
    app = app_ctx
    staff = _seed_user("tender.ops.update@example.com", role="operations_staff")
    tender = _seed_tender(created_by=staff.id, bid_manager_user_id=staff.id, status="Awarded")
    checklist_item = TenderChecklistItem.query.filter_by(tender_id=tender.id, item_key="sbd_4").first()
    assert checklist_item is not None
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    update_response = client.post(
        f"/tenders/{tender.id}/checklist/{checklist_item.id}",
        data={
            "csrf_token": "test-csrf",
            "is_required": "1",
            "status": "ready",
            "notes": "Signed and included in final pack.",
        },
        follow_redirects=False,
    )

    assert update_response.status_code == 302
    db.session.refresh(checklist_item)
    assert checklist_item.status == "ready"
    assert "final pack" in (checklist_item.notes or "")

    convert_response = client.post(
        f"/tenders/{tender.id}/matter",
        data={"csrf_token": "test-csrf"},
        follow_redirects=False,
    )
    assert convert_response.status_code == 403
    db.session.refresh(tender)
    assert tender.matter_id is None


def test_lawyer_can_convert_awarded_tender_to_matter(app_ctx):
    app = app_ctx
    lawyer = _seed_user("tender.lawyer@example.com", role="senior_attorney")
    tender = _seed_tender(created_by=lawyer.id, bid_manager_user_id=lawyer.id, status="Awarded")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)
    response = client.post(
        f"/tenders/{tender.id}/matter",
        data={"csrf_token": "test-csrf"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(tender)
    assert tender.matter_id is not None

    matter = db.session.get(Matter, tender.matter_id)
    assert matter is not None
    assert matter.practice_area == "Public Procurement"
    assert matter.case_type == "Tender Delivery"
    assert matter.client_name == tender.issuing_authority

    member = MatterMember.query.filter_by(matter_id=matter.id, user_id=lawyer.id).first()
    assert member is not None
    assert f"/matters/{matter.id}/workspace" in (response.headers.get("Location") or "")


def test_tender_create_rejects_duplicate_reference_for_same_authority(app_ctx):
    app = app_ctx
    staff = _seed_user("tender.ops.dupe@example.com", role="operations_staff")
    existing = _seed_tender(created_by=staff.id, bid_manager_user_id=staff.id, status="Sourced")
    existing.reference_no = "RFP 12/2026"
    existing.issuing_authority = "Department of Employment and Labour"
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        "/tenders",
        data={
            "csrf_token": "test-csrf",
            "reference_no": "RFP 12/2026",
            "title": "Duplicate legal panel capture",
            "issuing_authority": "Department of Employment and Labour",
            "province": "National",
            "closing_at": "2026-05-20T11:00",
            "briefing_required": "1",
            "bid_manager_user_id": str(staff.id),
        },
        follow_redirects=True,
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "already exists" in body
    assert TenderOpportunity.query.filter_by(
        reference_no="RFP 12/2026",
        issuing_authority="Department of Employment and Labour",
    ).count() == 1


def test_tender_update_rejects_briefing_after_closing_date(app_ctx):
    app = app_ctx
    staff = _seed_user("tender.ops.dates@example.com", role="operations_staff")
    tender = _seed_tender(created_by=staff.id, bid_manager_user_id=staff.id, status="Preparing Response")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        f"/tenders/{tender.id}",
        data={
            "csrf_token": "test-csrf",
            "reference_no": tender.reference_no,
            "title": tender.title,
            "issuing_authority": tender.issuing_authority,
            "province": tender.province,
            "status": tender.status,
            "tender_type": tender.tender_type,
            "portal_source": tender.portal_source,
            "sector": tender.sector or "",
            "bid_manager_user_id": str(staff.id),
            "closing_at": "2026-05-20T11:00",
            "briefing_required": "1",
            "briefing_date": "2026-05-21T09:00",
            "validity_end_date": "2026-06-20",
            "estimated_value": "",
            "preference_system": "",
            "submission_channel": "",
            "etender_url": "",
            "contact_person": "",
            "contact_email": "",
            "contact_phone": "",
            "csd_supplier_number": "",
            "tcs_pin": "",
            "bbbee_level": "",
            "cidb_grading": "",
            "submission_address": "",
            "scope_summary": "",
            "next_action": "",
            "internal_notes": "",
        },
        follow_redirects=True,
    )

    body = response.get_data(as_text=True)
    db.session.refresh(tender)
    assert response.status_code == 200
    assert "Briefing date cannot be after the closing date and time." in body
    assert tender.briefing_required is False
    assert tender.briefing_date is None
