"""When a claim is complex enough for the stronger model, and how a second reading combines with the first.

A second reading can add information but can't remove a warning. Otherwise a document could get past a flag simply
by looking unclear to the first model and clean to the second.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from claims.config import Settings
from claims.consolidation import FREE_TEXT, normalise_field
from claims.routing import CONFIDENCE_CHECKED

# The fields routing checks for confidence. A shaky amount_claimed doesn't count: routing relies on the invoice total.
_DECISIVE_FIELDS = {*CONFIDENCE_CHECKED, "invoice_total"}


def second_reading_reason(reading: dict[str, Any], threshold: float) -> str | None:
    """Why this document should be read again, or None. Anything already flagged goes to a person, so isn't worth re-reading."""
    if reading["embedded_instructions"] or reading["concerns"]:
        return None
    if not reading["readable"]:
        return "marked unreadable"
    unsure = sorted(f["name"] for f in reading["fields"] if f["name"] in _DECISIVE_FIELDS and f["confidence"] < threshold)
    return f"low confidence on {', '.join(unsure)}" if unsure else None


def _comparable(name: str, value: str) -> Any:
    normalised = normalise_field(name, value)
    return normalised[0] if normalised else value


def merge_readings(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """The second reading, plus every warning and field from the first. Disagreement on a value becomes a concern."""
    concerns = [*first["concerns"], *second["concerns"]]
    fields = list(second["fields"])
    by_name = {f["name"]: f for f in second["fields"]}
    for field in first["fields"]:
        other = by_name.get(field["name"])
        if other is None:
            fields.append(field)
        elif field["name"] not in FREE_TEXT and _comparable(field["name"], field["value"]) != _comparable(other["name"], other["value"]):
            concerns.append(f"Two readings disagree on {field['name']}: '{field['value']}' and '{other['value']}'.")
    return {
        **second,
        "fields": fields,
        "concerns": concerns,
        "embedded_instructions": first["embedded_instructions"] or second["embedded_instructions"],
    }


def coverage_model_for(claim: dict[str, Any], any_reread: bool, already_flagged: bool, settings: Settings) -> str:
    """Which model checks coverage. A claim already going to a person doesn't need the stronger one."""
    if already_flagged:
        return settings.model
    high_value = claim["amount"] is not None and Decimal(claim["amount"]) >= settings.complex_amount
    return settings.complex_model if high_value or any_reread else settings.model
