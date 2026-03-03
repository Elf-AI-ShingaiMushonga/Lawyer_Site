from __future__ import annotations

from ..models import ContractTemplate, Matter, MatterTemplate, PracticeArea


DEFAULT_LEGAL_CATEGORIES: tuple[str, ...] = (
    "General Legal",
    "Labour Law",
    "Commercial Law",
    "Corporate Law",
    "Litigation",
    "Property Law",
    "Compliance",
    "Tax Law",
    "Family Law",
    "Intellectual Property",
)


def _normalized_unique(values: list[str | None]) -> list[str]:
    items = {
        str(value).strip()
        for value in values
        if isinstance(value, str) and str(value).strip()
    }
    return sorted(items)


def legal_category_options(*, extra_values: list[str | None] | None = None) -> list[str]:
    raw_values: list[str | None] = list(DEFAULT_LEGAL_CATEGORIES)
    raw_values.extend(
        value
        for (value,) in MatterTemplate.query.with_entities(MatterTemplate.legal_category)
        .filter(MatterTemplate.legal_category.isnot(None))
        .distinct()
        .all()
    )
    raw_values.extend(
        value
        for (value,) in ContractTemplate.query.with_entities(ContractTemplate.legal_category)
        .filter(ContractTemplate.legal_category.isnot(None))
        .distinct()
        .all()
    )
    raw_values.extend(
        value
        for (value,) in Matter.query.with_entities(Matter.legal_category)
        .filter(Matter.legal_category.isnot(None))
        .distinct()
        .all()
    )
    if extra_values:
        raw_values.extend(extra_values)
    return _normalized_unique(raw_values)


def practice_area_options(*, extra_values: list[str | None] | None = None) -> list[str]:
    raw_values: list[str | None] = []
    raw_values.extend(
        value
        for (value,) in PracticeArea.query.with_entities(PracticeArea.name)
        .filter(PracticeArea.is_active.is_(True))
        .distinct()
        .all()
    )
    raw_values.extend(
        value
        for (value,) in MatterTemplate.query.with_entities(MatterTemplate.practice_area)
        .filter(MatterTemplate.practice_area.isnot(None))
        .distinct()
        .all()
    )
    raw_values.extend(
        value
        for (value,) in Matter.query.with_entities(Matter.practice_area)
        .filter(Matter.practice_area.isnot(None))
        .distinct()
        .all()
    )
    if extra_values:
        raw_values.extend(extra_values)
    return _normalized_unique(raw_values)
