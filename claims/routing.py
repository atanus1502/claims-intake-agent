"""The fast-track decision. Plain rules, no model: a claim is fast-tracked only when there's no reason at all to send it to a person."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from claims.config import Settings
from claims.models import reason

RULES_VERSION = "rules-v2"
REQUIRED_FIELDS = ("claimant_name", "date_of_loss", "loss_type", "loss_description")
NEEDS_POLICE_REPORT = {"theft", "vandalism"}
NEEDS_EXPLICIT_NO_INJURY = {"collision"}

FAST_TRACK = "fast_track"
HUMAN_REVIEW = "human_review"


def decide(
    claim: dict[str, Any],
    coverage: dict[str, Any],
    intake_issues: list[dict[str, str]],
    received_at: str,
    settings: Settings,
) -> dict[str, Any]:
    fields = claim["fields"]
    reasons = [*intake_issues, *claim["issues"], *coverage["issues"]]

    for name in REQUIRED_FIELDS:
        if name not in fields:
            reasons.append(reason("missing_field", f"{name} wasn't found in any document."))
    # The invoice total is the amount relied on, so it's the one that must be confident. Consolidation has already
    # flagged any claimed amount that disagrees with it, however confident either reading was.
    amount_source = "invoice_total" if "invoice_total" in fields else "amount_claimed"
    for name, field in fields.items():
        if name in (*REQUIRED_FIELDS, "policy_number", amount_source) and field["confidence"] < settings.confidence_threshold:
            reasons.append(reason("low_confidence", f"{name} has confidence {field['confidence']:.2f}."))

    loss_type = fields.get("loss_type", {}).get("value")
    if "invoice" not in claim["doc_types"] or "invoice_total" not in fields:
        reasons.append(reason("no_invoice", "No readable invoice supports the amount."))
    if loss_type in NEEDS_POLICE_REPORT and ("police_report" not in claim["doc_types"] or "police_report_number" not in fields):
        reasons.append(reason("no_police_report", f"A {loss_type} claim needs a police report."))

    if fields.get("injury_reported", {}).get("value") == "true":
        reasons.append(reason("injury_reported", "An injury was reported."))
    if fields.get("third_party_involved", {}).get("value") == "true":
        reasons.append(reason("third_party_involved", "Another party is involved."))
    if loss_type in NEEDS_EXPLICIT_NO_INJURY and not {"injury_reported", "third_party_involved"} <= fields.keys():
        reasons.append(reason("injury_status_unknown", "The claim doesn't say whether anyone was hurt or another party was involved."))

    if claim["amount"] is None:
        reasons.append(reason("missing_field", "No claim amount was found."))
    else:
        amount = Decimal(claim["amount"])
        if amount > settings.fast_track_limit:
            reasons.append(reason("over_fast_track_limit", f"{amount} is over the fast-track limit of {settings.fast_track_limit}."))
        if coverage.get("limit") is not None:
            payable = amount - Decimal(coverage["deductible"])
            if payable > Decimal(coverage["limit"]):
                reasons.append(reason("over_coverage_limit", f"{payable} after the deductible is over the limit of {coverage['limit']}."))
            if payable <= 0:
                reasons.append(reason("below_deductible", f"{amount} doesn't exceed the deductible of {coverage['deductible']}."))

    if "date_of_loss" in fields:
        loss_date = date.fromisoformat(fields["date_of_loss"]["value"])
        received = date.fromisoformat(received_at[:10])
        if loss_date > received:
            reasons.append(reason("loss_in_future", f"Date of loss {loss_date} is after the claim arrived."))
        elif (received - loss_date).days > settings.late_report_days:
            reasons.append(reason("late_report", f"Reported {(received - loss_date).days} days after the loss."))

    return {"route": HUMAN_REVIEW if reasons else FAST_TRACK, "reasons": reasons, "rules_version": RULES_VERSION}
