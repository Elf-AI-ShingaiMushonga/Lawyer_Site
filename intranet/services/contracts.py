from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterable, Mapping

from flask import current_app
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import sha256_file
from ..models import (
    ContractTemplate,
    DocumentFile,
    DocumentOCRText,
    DocumentRecord,
    DocumentVersion,
    Matter,
    MatterTemplate,
)
from .archetypes import build_document_context, load_required_fields, normalize_archetype_field_key, render_template_text


def auto_contract_templates_for_archetype(archetype_id: int | None) -> list[ContractTemplate]:
    if not archetype_id:
        return []
    return (
        ContractTemplate.query.filter(
            ContractTemplate.archetype_id == int(archetype_id),
            ContractTemplate.is_active.is_(True),
            ContractTemplate.auto_create_on_matter_open.is_(True),
        )
        .order_by(ContractTemplate.name.asc())
        .all()
    )


def contract_required_fields_union(templates: Iterable[ContractTemplate]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for template in templates:
        for field in load_required_fields(template.required_fields_json):
            key = normalize_archetype_field_key(str(field.get("key") or ""))
            if not key:
                continue
            label = str(field.get("label") or key.replace("_", " ").title()).strip()
            help_text = str(field.get("help") or "").strip()
            row = merged.get(key)
            if row is None:
                row = {"key": key, "label": label, "help": help_text, "templates": template.name}
                merged[key] = row
                continue
            existing_templates = str(row.get("templates") or "").strip()
            template_list = [part.strip() for part in existing_templates.split(",") if part.strip()]
            if template.name not in template_list:
                template_list.append(template.name)
            row["templates"] = ", ".join(template_list)
            if not row.get("help") and help_text:
                row["help"] = help_text
    return list(merged.values())


def collect_contract_field_values(form_data: Mapping[str, str], field_defs: list[dict[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in field_defs:
        key = normalize_archetype_field_key(str(field.get("key") or ""))
        if not key:
            continue
        raw_value = form_data.get(f"contract_field_{key}")
        value = str(raw_value or "").strip()
        if value:
            values[key] = value
    return values


def validate_contract_field_values(field_defs: list[dict[str, str]], values: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    for field in field_defs:
        key = normalize_archetype_field_key(str(field.get("key") or ""))
        if not key:
            continue
        value = str(values.get(key) or "").strip()
        if not value:
            missing.append(str(field.get("label") or key))
    return missing


def render_contract_template_for_matter(
    *,
    template: ContractTemplate,
    matter: Matter,
    archetype: MatterTemplate | None = None,
    archetype_values: Mapping[str, str] | None = None,
    contract_values: Mapping[str, str] | None = None,
) -> tuple[str, list[str]]:
    merged_values: dict[str, str] = {}
    if archetype_values:
        merged_values.update({str(k): str(v) for k, v in archetype_values.items()})
    if contract_values:
        merged_values.update({str(k): str(v) for k, v in contract_values.items()})
    context = build_document_context(matter, archetype=archetype, required_values=merged_values)
    context["contract_template_name"] = template.name or ""
    return render_template_text(template.body, context)


def persist_generated_contract_document(
    *,
    matter: Matter,
    template: ContractTemplate,
    rendered_body: str,
    actor_user_id: int,
    actor_full_name: str | None,
) -> tuple[DocumentRecord, DocumentVersion, str]:
    upload_dir = str(current_app.config.get("UPLOAD_DIR") or "").strip()
    if not upload_dir:
        raise RuntimeError("UPLOAD_DIR is not configured.")
    os.makedirs(upload_dir, exist_ok=True)

    base_name = secure_filename(f"{matter.matter_no}_{template.name}_contract") or f"matter_{matter.id}_contract_{template.id}"
    safe_name = f"{base_name[:80]}.txt"
    stored_filename = f"dms_{matter.id}_{uuid.uuid4().hex}_{safe_name}"
    file_path = os.path.join(upload_dir, stored_filename)
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write(rendered_body)

    sha = sha256_file(file_path)
    document = DocumentRecord(
        matter_id=matter.id,
        title=f"{template.name} - {matter.matter_no}",
        document_type=(template.contract_type or "Contract").strip() or "Contract",
        confidentiality="Internal",
        created_by=actor_user_id,
    )
    db.session.add(document)
    db.session.flush()

    legacy_file = DocumentFile(
        matter_id=matter.id,
        original_filename=safe_name,
        stored_filename=stored_filename,
        sha256=sha,
        content_type="text/plain",
        category=document.document_type,
        doc_version="1",
        lifecycle_stage="Draft",
        owner_name=actor_full_name or None,
        is_privileged=False,
        uploaded_by=actor_user_id,
    )
    db.session.add(legacy_file)
    db.session.flush()

    chain_hash = hashlib.sha256(f"GENESIS:{sha}".encode("utf-8")).hexdigest()
    version = DocumentVersion(
        document_id=document.id,
        document_file_id=legacy_file.id,
        version_no=1,
        original_filename=safe_name,
        stored_filename=stored_filename,
        sha256=sha,
        hash_chain_prev=None,
        hash_chain_current=chain_hash,
        state="draft",
        notes=f"Auto-generated from contract template '{template.name}' on matter creation.",
        uploaded_by=actor_user_id,
    )
    db.session.add(version)
    db.session.flush()

    db.session.add(
        DocumentOCRText(
            document_version_id=version.id,
            extracted_text=(rendered_body.strip() or "No generated text."),
        )
    )
    return document, version, file_path


def cleanup_generated_files(paths: Iterable[str]) -> None:
    for path in paths:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            continue
