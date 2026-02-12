from __future__ import annotations

from flask import abort, current_app

from ..models import DataResidencyPolicy


def _normalized_region(region: str | None) -> str:
    return (region or "").strip().upper()


def residency_allowed(data_class: str, target_region: str | None = None) -> tuple[bool, str | None]:
    normalized_data_class = (data_class or "").strip().lower()
    if not normalized_data_class:
        return True, None

    configured_region = _normalized_region(target_region or current_app.config.get("DATA_REGION"))
    if not configured_region:
        configured_region = "ZA"

    policies = (
        DataResidencyPolicy.query.filter(
            DataResidencyPolicy.is_active.is_(True),
            DataResidencyPolicy.data_class == normalized_data_class,
        )
        .order_by(DataResidencyPolicy.id.desc())
        .all()
    )
    if not policies:
        return True, None

    allowed_regions = {_normalized_region(p.region_code) for p in policies if _normalized_region(p.region_code)}
    if configured_region in allowed_regions:
        return True, None

    allowed_text = ", ".join(sorted(allowed_regions))
    return (
        False,
        f"Data residency policy blocks {normalized_data_class} in region {configured_region}. "
        f"Allowed region(s): {allowed_text}",
    )


def enforce_data_residency(data_class: str, target_region: str | None = None) -> None:
    allowed, message = residency_allowed(data_class, target_region=target_region)
    if not allowed:
        abort(403, description=message or "Data residency policy denied this operation.")
