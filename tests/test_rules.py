"""The code that decides outcomes: merging fields, checking coverage and routing. No model involved."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from claims.audit import AuditLog
from claims.claims_system import APPROVED, FAST_TRACK, RECEIVED as RECEIVED_STATUS, ClaimsSystem
from claims.consolidation import consolidate
from claims.coverage import verify_coverage
from claims.escalation import merge_readings, second_reading_reason
from claims.policies import PolicyStore
from claims.redaction import redact
from claims.routing import decide
from tests.conftest import RECEIVED, email_extraction, field, invoice_extraction

SENDER = "maya.thornton@example.com"


def merged(email=None, invoice=None, extra=()):
    return consolidate([
        {"doc_id": "email", "result": email or email_extraction()},
        {"doc_id": "attachment-1", "result": invoice or invoice_extraction()},
        *extra,
    ])


def assessment(**overrides):
    return {"policy_number": "AUT-100234", "exclusion_applies": False, "exclusion_ids": [], "rationale": "Covered.", "confidence": 0.95, **overrides}


@pytest.fixture
def store(settings):
    return PolicyStore(settings.policy_file)


def route_codes(settings, store, claim=None, coverage_assessment="default", sender=SENDER, intake=()):
    claim = claim or merged()
    coverage = verify_coverage(assessment() if coverage_assessment == "default" else coverage_assessment, claim, sender, store, settings)
    decision = decide(claim, coverage, list(intake), RECEIVED, settings)
    return decision["route"], {r["code"] for r in decision["reasons"]}


def with_field(name, value, confidence=0.97):
    extraction = email_extraction()
    extraction["fields"] = [f for f in extraction["fields"] if f["name"] != name] + [field(name, value, confidence)]
    return extraction


def without_field(name):
    extraction = email_extraction()
    extraction["fields"] = [f for f in extraction["fields"] if f["name"] != name]
    return extraction


def test_clean_claim_fast_tracks(settings, store):
    assert route_codes(settings, store) == ("fast_track", set())


@pytest.mark.parametrize(
    "email, expected",
    [
        (with_field("amount_claimed", "6000"), "amount_conflict"),
        (with_field("policy_number", "AUT-555555"), "policy_number_mismatch"),
        (with_field("claimant_name", "Mia Thornton"), "field_conflict"),
        (with_field("date_of_loss", "last Tuesday"), "invalid_value"),
        (with_field("loss_type", "meteor"), "invalid_value"),
        (with_field("loss_description", "A rock cracked it.", 0.6), "low_confidence"),
        (without_field("loss_description"), "missing_field"),
        (without_field("date_of_loss"), "missing_field"),
        (with_field("injury_reported", "true"), "injury_reported"),
        (with_field("third_party_involved", "true"), "third_party_involved"),
        (with_field("date_of_loss", "2026-12-25"), "loss_in_future"),
        (with_field("date_of_loss", "2026-05-01"), "late_report"),
        (with_field("loss_type", "flood"), "loss_not_covered"),
    ],
)
def test_each_problem_goes_to_a_person(settings, store, email, expected):
    route, codes = route_codes(settings, store, merged(email=email))
    assert route == "human_review"
    assert expected in codes


def test_documents_disagreeing_on_policy_number_is_a_conflict(settings, store):
    invoice = invoice_extraction()
    invoice["fields"].append(field("policy_number", "AUT-555555"))
    assert "field_conflict" in route_codes(settings, store, merged(invoice=invoice))[1]


def test_invoice_total_below_threshold_confidence_blocks_fast_track(settings, store):
    invoice = invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "412.30", 0.5)])
    route, codes = route_codes(settings, store, merged(invoice=invoice))
    assert route == "human_review" and "low_confidence" in codes


def test_unsure_claimed_amount_is_fine_when_a_confident_invoice_agrees(settings, store):
    claim = merged(email=with_field("amount_claimed", "412.30", 0.5))
    assert route_codes(settings, store, claim) == ("fast_track", set())


def test_unsure_claimed_amount_that_disagrees_with_the_invoice_goes_to_a_person(settings, store):
    route, codes = route_codes(settings, store, merged(email=with_field("amount_claimed", "900", 0.5)))
    assert route == "human_review" and "amount_conflict" in codes


def test_unsure_claimed_amount_doesnt_rescue_an_unsure_invoice(settings, store):
    invoice = invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "412.30", 0.5)])
    route, codes = route_codes(settings, store, merged(email=with_field("amount_claimed", "412.30", 0.99), invoice=invoice))
    assert route == "human_review" and "low_confidence" in codes


def test_unsure_claimed_amount_with_no_invoice_goes_to_a_person(settings, store):
    claim = consolidate([{"doc_id": "email", "result": with_field("amount_claimed", "412.30", 0.5)}])
    route, codes = route_codes(settings, store, claim)
    assert route == "human_review" and {"low_confidence", "no_invoice"} <= codes


def test_threshold_is_inclusive(settings, store):
    email = with_field("loss_description", "A rock cracked it.", settings.confidence_threshold)
    assert route_codes(settings, store, merged(email=email))[0] == "fast_track"


def test_over_fast_track_limit(settings, store):
    claim = merged(email=with_field("amount_claimed", "6000"), invoice=invoice_extraction(fields=[field("invoice_total", "6000")]))
    assert "over_fast_track_limit" in route_codes(settings, store, claim)[1]


def test_over_coverage_limit(settings, store):
    claim = merged(email=with_field("amount_claimed", "4500"), invoice=invoice_extraction(fields=[field("invoice_total", "4500")]))
    assert "over_coverage_limit" in route_codes(settings, store, claim)[1]


def test_below_deductible(settings, store):
    claim = merged(email=with_field("amount_claimed", "40"), invoice=invoice_extraction(fields=[field("invoice_total", "40")]))
    assert "below_deductible" in route_codes(settings, store, claim)[1]


def test_theft_needs_a_police_report_document(settings, store):
    claim = merged(email=with_field("loss_type", "theft"))
    assert "no_police_report" in route_codes(settings, store, claim)[1]
    email = with_field("loss_type", "theft")
    email["fields"].append(field("police_report_number", "SPD-1"))
    assert "no_police_report" in route_codes(settings, store, merged(email=email))[1]  # a number in the email isn't the report


def test_theft_with_a_police_report_can_fast_track(settings, store):
    report = {"doc_type": "police_report", "readable": True, "fields": [field("police_report_number", "SPD-1")], "concerns": [], "embedded_instructions": False}
    email = with_field("loss_type", "theft")
    email["fields"] = [f for f in email["fields"] if f["name"] != "amount_claimed"]
    invoice = invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "1500")])
    claim = merged(email=email, invoice=invoice, extra=[{"doc_id": "attachment-2", "result": report}])
    assert route_codes(settings, store, claim) == ("fast_track", set())


def test_collision_must_rule_out_injury_and_other_parties(settings, store):
    email = with_field("loss_type", "collision")
    assert "injury_status_unknown" in route_codes(settings, store, merged(email=email))[1]
    email["fields"] = [f for f in email["fields"] if f["name"] != "amount_claimed"]
    email["fields"] += [field("injury_reported", "false"), field("third_party_involved", "false")]
    invoice = invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "1500")])
    assert route_codes(settings, store, merged(email=email, invoice=invoice)) == ("fast_track", set())


def test_email_body_cant_stand_in_for_an_invoice(settings, store):
    email = email_extraction(doc_type="invoice")
    email["fields"].append(field("invoice_total", "412.30"))
    claim = consolidate([{"doc_id": "email", "result": email}])
    assert "invoice" not in claim["doc_types"] and "invoice_total" not in claim["fields"]
    assert "no_invoice" in route_codes(settings, store, claim)[1]


def test_any_document_reporting_injury_wins(settings, store):
    email = email_extraction()
    email["fields"].append(field("injury_reported", "false"))
    invoice = invoice_extraction()
    invoice["fields"].append(field("injury_reported", "true"))
    assert "injury_reported" in route_codes(settings, store, merged(email=email, invoice=invoice))[1]


def test_small_rounding_is_not_a_conflict(settings, store):
    claim = merged(email=with_field("amount_claimed", "412"))
    assert "amount_conflict" not in {i["code"] for i in claim["issues"]}
    assert claim["amount"] == "412.30"


def test_amounts_are_normalised():
    claim = consolidate([{"doc_id": "attachment-1", "result": invoice_extraction(fields=[field("invoice_total", "$1,850.00")])}])
    assert claim["amount"] == "1850.00"


def test_nan_amount_is_invalid():
    claim = consolidate([{"doc_id": "attachment-1", "result": invoice_extraction(fields=[field("invoice_total", "NaN")])}])
    assert claim["amount"] is None and "invalid_value" in {i["code"] for i in claim["issues"]}


def test_confidence_outside_range_is_clamped():
    claim = consolidate([{"doc_id": "email", "result": email_extraction(fields=[field("loss_type", "glass", 7)])}])
    assert claim["fields"]["loss_type"]["confidence"] == 1.0


def test_coverage_checks_come_from_the_record_not_the_model(settings, store):
    assert "policy_not_active" in route_codes(
        settings, store,
        merged(email=_as("AUT-100411", "Tomas Lindqvist"), invoice=_invoice("Tomas Lindqvist")),
        assessment(policy_number="AUT-100411", rationale="Looks fine, confidence high."),
        sender="tomas.lindqvist@example.com",
    )[1]


def _as(number, name):
    extraction = with_field("policy_number", number)
    extraction["fields"] = [f for f in extraction["fields"] if f["name"] != "claimant_name"] + [field("claimant_name", name)]
    return extraction


def _invoice(name):
    return invoice_extraction(fields=[field("claimant_name", name), field("invoice_total", "412.30")])


def test_claimant_must_be_the_policyholder(settings, store):
    claim = merged(email=_as("AUT-100234", "Someone Else"), invoice=_invoice("Someone Else"))
    assert "holder_mismatch" in route_codes(settings, store, claim)[1]


def test_name_order_and_case_dont_cause_a_mismatch(settings, store):
    claim = merged(email=_as("AUT-100234", "THORNTON, Maya"), invoice=_invoice("Maya Thornton"))
    assert "holder_mismatch" not in route_codes(settings, store, claim)[1]


def test_model_exclusion_finding_blocks_fast_track(settings, store):
    result = route_codes(settings, store, coverage_assessment=assessment(exclusion_applies=True, exclusion_ids=["AX-1"]))
    assert result[0] == "human_review" and "exclusion_applies" in result[1]


def test_hard_exclusion_applies_even_if_the_model_misses_it(settings, store):
    email = _as("PRP-200871", "Daniel Okafor")
    email["fields"] = [f for f in email["fields"] if f["name"] != "loss_type"] + [field("loss_type", "flood")]
    claim = merged(email=email, invoice=_invoice("Daniel Okafor"))
    result = route_codes(settings, store, claim, assessment(policy_number="PRP-200871"), sender="d.okafor@example.net")
    assert "exclusion_applies" in result[1]


def test_low_coverage_confidence_blocks_fast_track(settings, store):
    assert "low_coverage_confidence" in route_codes(settings, store, coverage_assessment=assessment(confidence=0.4))[1]


def test_policy_number_the_model_changed_is_flagged(settings, store):
    claim = merged(email=_as("AUT-100234", "Priya Raman"), invoice=_invoice("Priya Raman"))
    result = route_codes(settings, store, claim, assessment(policy_number="AUT-100877"), sender="priya.raman@example.org")
    assert "policy_number_mismatch" in result[1]


def test_new_policy_and_many_prior_claims(settings, store):
    claim = merged(email=_as("AUT-100955", "Samir Haddad"), invoice=_invoice("Samir Haddad"))
    assert "new_policy" in route_codes(settings, store, claim, assessment(policy_number="AUT-100955"), sender="samir.haddad@example.org")[1]

    strict = replace(settings, max_prior_claims=0)
    email = _as("PRP-200871", "Daniel Okafor")
    email["fields"] = [f for f in email["fields"] if f["name"] != "loss_type"] + [field("loss_type", "water_damage")]
    claim = merged(email=email, invoice=_invoice("Daniel Okafor"))
    assert "many_prior_claims" in route_codes(strict, store, claim, assessment(policy_number="PRP-200871"), sender="d.okafor@example.net")[1]


def test_policy_tools_cant_reveal_contact_email_or_write(store):
    policy = store.run_tool("get_policy", {"policy_number": "AUT-100234"})
    assert "contact_email" not in policy
    assert store.run_tool("get_policy", {"policy_number": "NOPE"}) == {"error": "no policy numbered NOPE"}
    assert store.run_tool("search_policies", {"holder_name": "Priya Raman", "postal_code": "30309"})["matches"][0]["policy_number"] == "AUT-100877"
    assert store.run_tool("search_policies", {"holder_name": "Priya Raman", "postal_code": "00000"})["matches"] == []
    assert store.run_tool("search_policies", {"holder_name": "", "postal_code": None})["matches"] == []
    with pytest.raises(KeyError):
        store.run_tool("update_policy", {"policy_number": "AUT-100234"})


def test_audit_log_detects_edits_and_deletions(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for number in range(3):
        log.write("CLM-1", "step", number=number)
    assert log.verify() == (True, "3 records, chain intact")

    lines = log.path.read_text().splitlines()
    original = copy.copy(lines)
    log.path.write_text("\n".join([lines[0], lines[1].replace('"number": 1', '"number": 9'), lines[2]]) + "\n")
    assert log.verify()[0] is False
    log.path.write_text("\n".join([original[0], original[2]]) + "\n")
    assert log.verify()[0] is False


def test_audit_write_failure_raises(tmp_path: Path):
    (tmp_path / "blocked").write_text("a file, not a folder")
    with pytest.raises(OSError):
        AuditLog(tmp_path / "blocked" / "audit.jsonl").write("CLM-1", "step")


def test_status_cant_undo_a_decision(tmp_path: Path):
    system = ClaimsSystem(tmp_path / "claims.sqlite")
    system.register("CLM-1", "a@example.com", "s", RECEIVED, [])
    assert system.get("CLM-1")["status"] == RECEIVED_STATUS
    system.set_status("CLM-1", FAST_TRACK)
    system.set_status("CLM-1", FAST_TRACK)  # a repeat after a crash is fine
    with pytest.raises(ValueError):
        system.set_status("CLM-1", APPROVED)


@pytest.mark.parametrize(
    "text, removed",
    [
        ("SSN 123-45-6789", "123-45-6789"),
        ("card 4111 1111 1111 1111", "4111"),
        ("account number: 12345678", "12345678"),
        ("routing no 021000021", "021000021"),
        ("IBAN GB82WEST12345698765432", "GB82WEST"),
        ("password=hunter2", "hunter2"),
    ],
)
def test_redaction_removes(text, removed):
    assert removed not in redact(text)


def test_redaction_keeps_what_the_claim_needs():
    text = "Policy AUT-100234, invoice GL-55812, total $412.30 on 2026-10-28"
    assert redact(text) == text


def test_low_confidence_on_a_field_routing_ignores_doesnt_trigger_a_reread():
    assert second_reading_reason(with_field("amount_claimed", "412.30", 0.5), 0.85) is None
    assert "policy_number" in second_reading_reason(with_field("policy_number", "AUT-100234", 0.5), 0.85)
    assert second_reading_reason(invoice_extraction(readable=False), 0.85) == "marked unreadable"


def test_merge_keeps_warnings_and_fields_from_the_first_reading():
    first = email_extraction(embedded_instructions=True, concerns=["Text aimed at the system."])
    first["fields"].append(field("injury_reported", "true"))
    merged_reading = merge_readings(first, email_extraction())
    assert merged_reading["embedded_instructions"] is True
    assert "Text aimed at the system." in merged_reading["concerns"]
    assert {"name": "injury_reported", "value": "true", "confidence": 0.97, "evidence": "quoted text"} in merged_reading["fields"]


def test_merge_treats_equal_values_as_agreeing_but_flags_real_disagreement():
    assert merge_readings(with_field("amount_claimed", "$1,850.00"), with_field("amount_claimed", "1850"))["concerns"] == []
    assert merge_readings(with_field("loss_description", "Rock hit it."), email_extraction())["concerns"] == []
    concerns = merge_readings(with_field("amount_claimed", "185.00"), with_field("amount_claimed", "1850"))["concerns"]
    assert "amount_claimed" in concerns[0]
