"""The fast-track decision. Plain rules, no model: a claim is fast-tracked only when there's no reason at all to send it to a person."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from claims.models import reason
from claims.rules import NO_INJURY_STATEMENT, POLICE_REPORT, RuleSet

FAST_TRACK = "fast_track"
HUMAN_REVIEW = "human_review"


def decide(
    claim: dict[str, Any],
    coverage: dict[str, Any],
    intake_issues: list[dict[str, str]],
    received_at: str,
    rules: RuleSet,
) -> dict[str, Any]:
    fields = claim["fields"]
    reasons = [*intake_issues, *claim["issues"], *coverage["issues"]]
    line = coverage.get("line")
    line_rules = rules.for_line(line)

    if line is not None and line not in rules.lines:
        reasons.append(reason("no_rules_for_line", f"The rules don't cover the {line} line of business."))
    for name in line_rules.required_fields:
        if name not in fields:
            reasons.append(reason("missing_field", f"{name} wasn't found in any document."))
    # The invoice total is the amount relied on, so it's the one that must be confident. Consolidation has already
    # flagged any claimed amount that disagrees with it, however confident either reading was.
    amount_source = "invoice_total" if "invoice_total" in fields else "amount_claimed"
    for name, field in fields.items():
        if name in (*rules.confidence_checked, amount_source) and field["confidence"] < rules.confidence_threshold:
            reasons.append(reason("low_confidence", f"{name} has confidence {field['confidence']:.2f}."))

    loss_type = fields.get("loss_type", {}).get("value")
    requires = line_rules.requires(loss_type)
    if "invoice" not in claim["doc_types"] or "invoice_total" not in fields:
        reasons.append(reason("no_invoice", "No readable invoice supports the amount."))
    if "invoice" in claim["doc_types"] and "invoice_date" not in fields:
        reasons.append(reason("missing_field", "The invoice has no readable date."))
    if POLICE_REPORT in requires and ("police_report" not in claim["doc_types"] or "police_report_number" not in fields):
        reasons.append(reason("no_police_report", f"A {loss_type} claim needs a police report."))

    if "injury_reported" in line_rules.review_if_reported and fields.get("injury_reported", {}).get("value") == "true":
        reasons.append(reason("injury_reported", "An injury was reported."))
    if "third_party_involved" in line_rules.review_if_reported and fields.get("third_party_involved", {}).get("value") == "true":
        reasons.append(reason("third_party_involved", "Another party is involved."))
    if NO_INJURY_STATEMENT in requires and not {"injury_reported", "third_party_involved"} <= fields.keys():
        reasons.append(reason("injury_status_unknown", "The claim doesn't say whether anyone was hurt or another party was involved."))

    if claim["amount"] is None:
        reasons.append(reason("missing_field", "No claim amount was found."))
    else:
        amount = Decimal(claim["amount"])
        # The claimed amount counts too: the tolerance for rounding mustn't hide a claim over the limit.
        claimed = Decimal(fields["amount_claimed"]["value"]) if "amount_claimed" in fields else amount
        if max(amount, claimed) > line_rules.fast_track_limit:
            reasons.append(reason("over_fast_track_limit", f"{max(amount, claimed)} is over the fast-track limit of {line_rules.fast_track_limit}."))
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
        elif (received - loss_date).days > line_rules.late_report_days:
            reasons.append(reason("late_report", f"Reported {(received - loss_date).days} days after the loss."))
        if "invoice_date" in fields and date.fromisoformat(fields["invoice_date"]["value"]) < loss_date:
            reasons.append(reason("invoice_before_loss", f"Invoice dated {fields['invoice_date']['value']} is before the loss on {loss_date}."))

    return {
        "route": HUMAN_REVIEW if reasons else FAST_TRACK,
        "reasons": reasons,
        "rules_version": rules.version,
        "rules_digest": rules.digest,
    }
