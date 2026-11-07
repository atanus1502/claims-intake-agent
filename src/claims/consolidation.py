"""Merges per-document extractions into one claim, and records every disagreement between documents."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from claims.models import reason
from claims.policies import normalise_name

LOSS_TYPES = {"glass", "collision", "theft", "vandalism", "water_damage", "fire", "storm", "flood", "other"}
FREE_TEXT = {"loss_description", "loss_location"}
FLAGS = {"injury_reported", "third_party_involved"}
AMOUNT_TOLERANCE = Decimal("0.01")  # claimants round; "about $450" against a $447.50 invoice isn't a conflict


def parse_amount(value: str) -> Decimal | None:
    try:
        amount = Decimal(value.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def normalise_field(name: str, value: str) -> tuple[Any, str] | None:
    """The form to compare by and the form to store, or None if the value isn't valid for this field."""
    value = value.strip()
    if not value:
        return None
    if name == "date_of_loss":
        try:
            return date.fromisoformat(value), value
        except ValueError:
            return None
    if name in ("amount_claimed", "invoice_total"):
        amount = parse_amount(value)
        return (amount, str(amount)) if amount is not None else None
    if name == "loss_type":
        loss_type = value.casefold()
        return (loss_type, loss_type) if loss_type in LOSS_TYPES else None
    if name in FLAGS:
        flag = value.casefold()
        return (flag, flag) if flag in ("true", "false") else None
    if name == "claimant_name":
        return normalise_name(value), value
    if name in ("policy_number", "postal_code", "invoice_number", "police_report_number"):
        cleaned = value.replace(" ", "").upper()
        return cleaned, cleaned
    return value, value


def consolidate(extractions: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    doc_types: set[str] = set()

    for extraction in sorted(extractions, key=lambda e: e["doc_id"]):
        doc_id = extraction["doc_id"]
        if extraction.get("error"):
            issues.append(reason("extraction_failed", f"{doc_id} could not be read automatically."))
            continue
        result = extraction["result"]
        if not result["readable"]:
            issues.append(reason("unreadable_document", f"{doc_id} ({result['doc_type']}) couldn't be read reliably."))
            continue
        # The email body is the claimant's own account, whatever it looks like, so it can't count as an invoice or report.
        doc_type = "email" if doc_id == "email" else result["doc_type"]
        doc_types.add(doc_type)
        if result["embedded_instructions"]:
            issues.append(reason("embedded_instructions", f"{doc_id} contains text aimed at automated processing."))
        for concern in result["concerns"]:
            issues.append(reason("document_concern", f"{doc_id}: {concern}"))
        for field in result["fields"]:
            normalised = normalise_field(field["name"], field["value"])
            if normalised is None:
                issues.append(reason("invalid_value", f"{doc_id}: {field['name']} has an unusable value."))
                continue
            compare, stored = normalised
            confidence = min(max(float(field["confidence"]), 0.0), 1.0)
            name = "amount_claimed" if field["name"] == "invoice_total" and doc_type != "invoice" else field["name"]
            grouped.setdefault(name, []).append(
                {"compare": compare, "value": stored, "confidence": confidence, "source": doc_id}
            )

    fields: dict[str, dict[str, Any]] = {}
    for name, candidates in grouped.items():
        distinct = {c["compare"] for c in candidates}
        if name in FLAGS and len(distinct) > 1:
            # One document saying yes is enough; a person should see an injury that another document didn't mention.
            candidates = [c for c in candidates if c["compare"] == "true"]
        elif len(distinct) > 1 and name not in FREE_TEXT:
            values = ", ".join(f"{c['value']} ({c['source']})" for c in candidates)
            issues.append(reason("field_conflict", f"Documents disagree on {name}: {values}."))
        best = max(candidates, key=lambda c: c["confidence"])
        fields[name] = {
            "value": best["value"],
            "confidence": best["confidence"],
            "sources": sorted({c["source"] for c in candidates if c["compare"] == best["compare"]}),
        }

    amount_field = fields.get("invoice_total") or fields.get("amount_claimed")
    if "invoice_total" in fields and "amount_claimed" in fields:
        invoice = Decimal(fields["invoice_total"]["value"])
        claimed = Decimal(fields["amount_claimed"]["value"])
        if abs(invoice - claimed) > invoice * AMOUNT_TOLERANCE:
            issues.append(reason("amount_conflict", f"Claimed {claimed} but the invoice total is {invoice}."))

    return {
        "fields": fields,
        "amount": amount_field["value"] if amount_field else None,
        "amount_confidence": amount_field["confidence"] if amount_field else 0.0,
        "doc_types": sorted(doc_types),
        "issues": issues,
    }
