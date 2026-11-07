"""Checks Claude's coverage assessment against the policy record. Dates, limits and identity are settled here, not by the model."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from claims.models import reason
from claims.policies import PolicyStore, normalise_name, prior_claims_within
from claims.rules import RuleSet


def verify_coverage(
    assessment: dict[str, Any] | None,
    claim: dict[str, Any],
    sender: str,
    store: PolicyStore,
    rules: RuleSet,
) -> dict[str, Any]:
    if assessment is None:
        return {"issues": [reason("coverage_not_assessed", "The coverage check did not finish.")]}

    fields = claim["fields"]
    issues: list[dict[str, str]] = []
    policy = store.get(assessment["policy_number"]) if assessment["policy_number"] else None
    if policy is None:
        return {"issues": [reason("policy_not_found", "No policy could be matched to this claim.")]}
    line_rules = rules.for_line(policy["line"])

    stated_number = fields.get("policy_number", {}).get("value")
    if stated_number and stated_number != policy["policy_number"].upper():
        issues.append(reason("policy_number_mismatch", f"Claim states {stated_number}; matched {policy['policy_number']}."))

    claimant = fields.get("claimant_name", {}).get("value", "")
    if normalise_name(claimant) != normalise_name(policy["holder_name"]):
        issues.append(reason("holder_mismatch", f"Claimant '{claimant}' isn't the policyholder on {policy['policy_number']}."))
    if sender.casefold() != policy["contact_email"].casefold():
        issues.append(reason("sender_mismatch", "The email didn't come from the address on the policy."))
    if policy["status"] not in line_rules.active_policy_statuses:
        issues.append(reason("policy_not_active", f"Policy status is {policy['status']}."))

    loss_type = fields.get("loss_type", {}).get("value")
    coverage = policy["coverages"].get(loss_type) if loss_type else None
    if coverage is None:
        issues.append(reason("loss_not_covered", f"The policy has no coverage for loss type '{loss_type}'."))

    for exclusion in policy.get("exclusions", []):
        if loss_type in exclusion.get("loss_types", []):
            issues.append(reason("exclusion_applies", f"{exclusion['id']}: {exclusion['text']}"))
    if assessment["exclusion_applies"]:
        issues.append(reason("exclusion_applies", f"{', '.join(assessment['exclusion_ids'])}: {assessment['rationale']}"))

    if "date_of_loss" in fields:
        loss_date = date.fromisoformat(fields["date_of_loss"]["value"])
        if not policy["inception"] <= loss_date <= policy["expiry"]:
            issues.append(reason("outside_policy_period", f"Loss on {loss_date} is outside {policy['inception']} to {policy['expiry']}."))
        if loss_date - policy["inception"] < timedelta(days=line_rules.new_policy_days):
            issues.append(reason("new_policy", f"Loss within {line_rules.new_policy_days} days of the policy starting."))
        prior = prior_claims_within(policy, loss_date, line_rules.prior_claims_window_days)
        if prior > line_rules.max_prior_claims:
            issues.append(reason("many_prior_claims", f"{prior} claims in the {line_rules.prior_claims_window_days} days before this loss."))

    if min(max(float(assessment["confidence"]), 0.0), 1.0) < rules.confidence_threshold:
        issues.append(reason("low_coverage_confidence", f"Coverage check confidence {assessment['confidence']}: {assessment['rationale']}"))

    return {
        "policy_number": policy["policy_number"],
        "line": policy["line"],
        "limit": str(coverage["limit"]) if coverage else None,
        "deductible": str(coverage["deductible"]) if coverage else None,
        "rationale": assessment["rationale"],
        "issues": issues,
    }
