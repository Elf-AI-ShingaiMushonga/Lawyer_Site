from __future__ import annotations

import os
import re
import uuid

from werkzeug.utils import secure_filename

_KIND_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_stored_filename(stored_filename: str | None) -> str:
    raw = str(stored_filename or "").strip().replace("\\", "/")
    raw = raw.split("\x00", 1)[0].lstrip("/")
    if not raw:
        raise ValueError("Stored filename is empty.")
    normalized = os.path.normpath(raw).replace("\\", "/")
    if normalized in {"", "."}:
        raise ValueError("Stored filename is invalid.")
    if normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError("Stored filename escapes upload root.")
    return normalized


def resolve_upload_path(
    upload_root: str | None,
    stored_filename: str | None,
    *,
    create_parent: bool = False,
) -> tuple[str, str]:
    root = os.path.abspath(str(upload_root or "").strip())
    if not root:
        raise ValueError("UPLOAD_DIR is not configured.")
    normalized = normalize_stored_filename(stored_filename)
    abs_path = os.path.abspath(os.path.join(root, normalized))
    if abs_path != root and not abs_path.startswith(root + os.sep):
        raise ValueError("Stored filename escapes upload root.")
    if create_parent:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    return normalized, abs_path


def build_matter_storage_name(kind: str, matter_id: int, original_filename: str) -> str:
    kind_slug = _KIND_RE.sub("", str(kind or "").strip().lower()) or "dms"
    try:
        matter_num = max(0, int(matter_id))
    except (TypeError, ValueError):
        matter_num = 0
    shard = f"m{matter_num % 1000:03d}"
    safe_name = secure_filename(str(original_filename or "").strip())[:120] or f"{kind_slug}_file"
    return f"{kind_slug}/{shard}/matter_{matter_num}/{uuid.uuid4().hex}_{safe_name}"
