from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any

from flask import current_app

from ..extensions import db


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9&.\-]{1,}")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"\b(?:\+?\d[\s\-()]?){8,}\d\b")
_CASE_RE = re.compile(r"\b\d{4}-[A-Z]{2,}-\d{2,}\b", re.IGNORECASE)


def _tokenize(value: str) -> set[str]:
    if not value:
        return set()
    return {token for token in _TOKEN_RE.findall(value.lower()) if token}


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    norm_l = math.sqrt(sum(x * x for x in left))
    norm_r = math.sqrt(sum(x * x for x in right))
    if norm_l <= 0 or norm_r <= 0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    return dot / (norm_l * norm_r)


def _fallback_embedding(text: str, *, dimensions: int) -> list[float]:
    tokens = _tokenize(text)
    vector = [0.0] * dimensions
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = digest[0] % dimensions
        sign = -1.0 if digest[1] % 2 else 1.0
        weight = 1.0 + min(2.0, len(token) / 12.0)
        vector[bucket] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _redact_text(text: str) -> tuple[str, dict[str, int]]:
    counters = {"email": 0, "phone": 0, "case_ref": 0}
    redacted, counters["email"] = _EMAIL_RE.subn("[REDACTED_EMAIL]", text or "")
    redacted, counters["phone"] = _PHONE_RE.subn("[REDACTED_PHONE]", redacted)
    redacted, counters["case_ref"] = _CASE_RE.subn("[REDACTED_CASE_REF]", redacted)
    return redacted, counters


def _merge_counters(items: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            merged[key] = int(merged.get(key, 0)) + int(value or 0)
    return merged


def log_ai_operation(
    *,
    operation_type: str,
    provider: str,
    model: str | None,
    status: str,
    request_chars: int,
    response_units: int | None = None,
    latency_ms: int | None = None,
    redaction_applied: bool = False,
    metadata: dict | None = None,
    error_message: str | None = None,
) -> None:
    if not bool(current_app.config.get("AI_OPERATION_LOGGING", True)):
        return
    try:
        from ..models import AIOperationLog

        db.session.add(
            AIOperationLog(
                operation_type=operation_type[:80],
                provider=(provider or "unknown")[:40],
                model=(model or None),
                status=(status or "unknown")[:20],
                request_chars=max(0, int(request_chars or 0)),
                response_units=int(response_units) if response_units is not None else None,
                latency_ms=int(latency_ms) if latency_ms is not None else None,
                redaction_applied=bool(redaction_applied),
                metadata_json=_safe_json_dumps(metadata or {}),
                error_message=(str(error_message)[:2000] if error_message else None),
            )
        )
    except Exception:  # pragma: no cover - defensive logging guard
        current_app.logger.exception("Failed to persist AI operation log")


def embed_texts(texts: list[str], *, operation_type: str) -> tuple[list[list[float]], dict[str, Any]]:
    normalized = [str(text or "") for text in texts]
    if not normalized:
        return [], {
            "provider": "none",
            "model": None,
            "fallback_used": True,
            "redaction_applied": False,
            "redaction_counts": {},
        }

    redact_before_embed = bool(current_app.config.get("AI_REDACT_BEFORE_EMBEDDING", True))
    redaction_stats: list[dict[str, int]] = []
    prepared: list[str] = []
    for item in normalized:
        if redact_before_embed:
            redacted, stats = _redact_text(item)
            prepared.append(redacted)
            redaction_stats.append(stats)
        else:
            prepared.append(item)
            redaction_stats.append({})
    merged_redaction = _merge_counters(redaction_stats)
    redaction_applied = bool(sum(merged_redaction.values()))

    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    fallback_dimensions = max(32, int(current_app.config.get("AI_FALLBACK_EMBED_DIMENSIONS", 256) or 256))
    strict_mode = bool(current_app.config.get("AI_EMBED_STRICT", False))
    request_chars = sum(len(item) for item in prepared)

    if ai_enabled and provider == "openai":
        api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
        model = str(current_app.config.get("AI_OPENAI_EMBED_MODEL") or "text-embedding-3-small").strip()
        dimensions = max(0, int(current_app.config.get("AI_OPENAI_EMBED_DIMENSIONS", 1024) or 0))
        timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
        max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
        started = time.perf_counter()
        if api_key:
            try:
                from openai import OpenAI

                client = OpenAI(api_key=api_key, timeout=float(timeout_seconds), max_retries=max_retries)
                payload: dict[str, Any] = {"model": model, "input": prepared}
                if dimensions > 0:
                    payload["dimensions"] = dimensions
                response = client.embeddings.create(**payload)
                vectors = [list(item.embedding or []) for item in response.data]
                latency_ms = int((time.perf_counter() - started) * 1000)
                log_ai_operation(
                    operation_type=operation_type,
                    provider="openai",
                    model=getattr(response, "model", None) or model,
                    status="ok",
                    request_chars=request_chars,
                    response_units=len(vectors),
                    latency_ms=latency_ms,
                    redaction_applied=redaction_applied,
                    metadata={"count": len(vectors), "dimensions": len(vectors[0]) if vectors else 0},
                )
                return vectors, {
                    "provider": "openai",
                    "model": getattr(response, "model", None) or model,
                    "fallback_used": False,
                    "redaction_applied": redaction_applied,
                    "redaction_counts": merged_redaction,
                }
            except Exception as exc:  # pragma: no cover - network/provider dependency
                latency_ms = int((time.perf_counter() - started) * 1000)
                log_ai_operation(
                    operation_type=operation_type,
                    provider="openai",
                    model=model,
                    status="error",
                    request_chars=request_chars,
                    latency_ms=latency_ms,
                    redaction_applied=redaction_applied,
                    metadata={"count": len(prepared)},
                    error_message=str(exc),
                )
                if strict_mode:
                    raise
                current_app.logger.warning("OpenAI embeddings unavailable, using fallback embeddings: %s", exc)
        elif strict_mode:
            raise RuntimeError("AI provider is openai but no API key is configured")

    vectors = [_fallback_embedding(item, dimensions=fallback_dimensions) for item in prepared]
    log_ai_operation(
        operation_type=operation_type,
        provider=f"{provider}_fallback",
        model=f"hashed_{fallback_dimensions}",
        status="ok",
        request_chars=request_chars,
        response_units=len(vectors),
        redaction_applied=redaction_applied,
        metadata={"count": len(vectors), "dimensions": fallback_dimensions},
    )
    return vectors, {
        "provider": f"{provider}_fallback",
        "model": f"hashed_{fallback_dimensions}",
        "fallback_used": True,
        "redaction_applied": redaction_applied,
        "redaction_counts": merged_redaction,
    }


def embedding_to_json(vector: list[float]) -> str:
    compact = [round(float(value), 10) for value in vector]
    return _safe_json_dumps(compact)


def embedding_from_json(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[float] = []
    for item in data:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "embed_texts",
    "embedding_from_json",
    "embedding_to_json",
    "log_ai_operation",
    "_cosine_similarity",
]
