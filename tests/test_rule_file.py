"""The rules file: that it holds the agreed values, that lines can differ, and that mistakes stop processing."""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal

import pytest
import yaml

from claims.config import Settings
from claims.consolidation import consolidate
from claims.coverage import verify_coverage
from claims.escalation import coverage_model_for
from claims.policies import PolicyStore
from claims.prompts import extraction_prompt, prompt_version
from claims.routing import decide
from claims.rules import RulesError, load_rules, parse_rules
from claims.service import AWAITING_REVIEW, ClaimsAgent
from tests.conftest import RECEIVED, ROOT, RULES, ScriptedLLM, email_extraction, field, invoice_extraction, make_email, submit_call

BASE = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())


def changed(edit):
    data = copy.deepcopy(BASE)
    edit(data)
    return data


def route(rules, amount="412.30", loss_type="glass", policy="AUT-100234", name="Maya Thornton", sender="maya.thornton@example.com", extra=()):
    email = email_extraction(fields=[
        field("policy_number", policy), field("claimant_name", name), field("date_of_loss", "2026-10-28"),
        field("loss_type", loss_type), field("loss_description", "Damage."), *extra,
    ])
    invoice = invoice_extraction(fields=[field("invoice_total", amount), field("invoice_date", "2026-10-29")])
    claim = consolidate([{"doc_id": "email", "result": email}, {"doc_id": "attachment-1", "result": invoice}], rules)
    assessment = {"policy_number": policy, "exclusion_applies": False, "exclusion_ids": [], "rationale": "Fine.", "confidence": 0.95}
    store = PolicyStore(ROOT / "config" / "mock_policies.yaml")
    decision = decide(claim, verify_coverage(assessment, claim, sender, store, rules), [], RECEIVED, rules)
    return decision["route"], {r["code"] for r in decision["reasons"]}, decision


def test_file_holds_the_agreed_values():
    assert RULES.confidence_threshold == 0.85
    assert RULES.amount_tolerance == Decimal("0.01")
    assert set(RULES.lines) == {"auto", "property"}
    for line in (RULES.defaults, *RULES.lines.values()):
        assert line.fast_track_limit == 5000
        assert (line.late_report_days, line.new_policy_days, line.max_prior_claims) == (60, 30, 2)
        assert line.required_fields == ("claimant_name", "date_of_loss", "loss_type", "loss_description")
        assert line.requires("theft") == line.requires("vandalism") == {"police_report"}
        assert line.requires("collision") == {"no_injury_statement"}
        assert line.requires("glass") == frozenset()
        assert line.prior_claims_window_days == 365
        assert line.active_policy_statuses == ("active",)
        assert line.review_if_reported == ("injury_reported", "third_party_involved")
    assert RULES.recognized_loss_types == ("glass", "collision", "theft", "vandalism", "water_damage", "fire", "storm", "flood", "other")
    assert (RULES.complex_amount, RULES.coverage_after_reread, RULES.redo_unsure_coverage) == (2000, True, True)


def test_decision_records_the_version_and_a_fingerprint_of_the_file():
    _, _, decision = route(RULES)
    assert decision["rules_version"] == BASE["version"]
    assert len(decision["rules_digest"]) == 12


def test_edit_without_a_version_bump_still_changes_the_fingerprint(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(changed(lambda d: d["defaults"].update(fast_track_limit=100))))
    assert load_rules(path).digest != RULES.digest


def test_limit_can_differ_by_line():
    rules = parse_rules(changed(lambda d: d["lines"].update(property={"fast_track_limit": 300})))
    assert rules.lines["auto"].fast_track_limit == 5000
    assert route(rules, amount="412.30")[0] == "fast_track"
    assert "over_fast_track_limit" in route(
        rules, policy="PRP-200871", name="Daniel Okafor", sender="d.okafor@example.net", loss_type="water_damage", amount="512.30"
    )[1]


def test_evidence_can_differ_by_line_and_other_requirements_are_kept():
    rules = parse_rules(changed(lambda d: d["lines"].update(auto={"loss_types": {"glass": {"requires": ["police_report"]}}})))
    assert "no_police_report" in route(rules)[1]
    assert rules.lines["auto"].requires("theft") == {"police_report"}
    assert rules.lines["property"].requires("glass") == frozenset()


def test_a_line_can_remove_a_requirement_only_by_saying_so():
    rules = parse_rules(changed(lambda d: d["lines"].update(auto={"loss_types": {"theft": {"requires": []}}})))
    assert rules.lines["auto"].requires("theft") == frozenset()
    assert rules.lines["property"].requires("theft") == {"police_report"}


def test_new_policy_window_can_differ_by_line():
    rules = parse_rules(changed(lambda d: d["lines"].update(auto={"new_policy_days": 900})))
    assert "new_policy" in route(rules)[1]


def test_policy_on_a_line_with_no_rules_goes_to_a_person():
    rules = parse_rules(changed(lambda d: d["lines"].pop("auto")))
    decision_route, codes, _ = route(rules)
    assert decision_route == "human_review" and "no_rules_for_line" in codes


def test_confidence_threshold_comes_from_the_file():
    rules = parse_rules(changed(lambda d: d.update(confidence_threshold=0.99)))
    assert "low_confidence" in route(rules)[1]


def test_amount_tolerance_comes_from_the_file():
    invoice = invoice_extraction(fields=[field("invoice_total", "100"), field("invoice_date", "2026-10-29")])
    email = email_extraction(fields=[field("amount_claimed", "105")])
    extractions = [{"doc_id": "email", "result": email}, {"doc_id": "attachment-1", "result": invoice}]
    assert "amount_conflict" in {i["code"] for i in consolidate(extractions, RULES)["issues"]}
    loose = parse_rules(changed(lambda d: d.update(amount_tolerance=0.1)))
    assert "amount_conflict" not in {i["code"] for i in consolidate(extractions, loose)["issues"]}


@pytest.mark.parametrize(
    "edit, message",
    [
        (lambda d: d.pop("version"), "missing: version"),
        (lambda d: d.update(version=""), "version"),
        (lambda d: d.pop("defaults"), "missing: defaults"),
        (lambda d: d.update(confidence_treshold=0.5), "unknown settings: confidence_treshold"),
        (lambda d: d.update(confidence_threshold=85), "out of range"),
        (lambda d: d.update(confidence_threshold="high"), "must be a number"),
        (lambda d: d.update(confidence_threshold=True), "must be a number"),
        (lambda d: d.update(amount_tolerance=-0.1), "out of range"),
        (lambda d: d.update(confidence_checked=["polcy_number"]), "unknown fields: polcy_number"),
        (lambda d: d.update(confidence_checked=["policy_number"]), "must also be in confidence_checked"),
        (lambda d: d["defaults"].update(fast_track_limt=1), "unknown settings: fast_track_limt"),
        (lambda d: d["defaults"].pop("fast_track_limit"), "missing fast_track_limit"),
        (lambda d: d["defaults"].pop("loss_types"), "missing loss_types"),
        (lambda d: d["defaults"].update(fast_track_limit=-1), "out of range"),
        (lambda d: d["defaults"].update(late_report_days=1.5), "whole number"),
        (lambda d: d["defaults"].update(required_fields=[]), "list of field names"),
        (lambda d: d["defaults"]["loss_types"].update(meteor={"requires": []}), "unknown settings: meteor"),
        (lambda d: d["defaults"]["loss_types"].update(theft={"requires": ["sworn_statement"]}), "unknown requirements: sworn_statement"),
        (lambda d: d["defaults"]["loss_types"].update(theft={}), "needs a list"),
        (lambda d: d.update(lines={}), "at least one line"),
        (lambda d: d.pop("opus"), "missing: opus"),
        (lambda d: d["opus"].pop("complex_amount"), "opus is missing: complex_amount"),
        (lambda d: d["opus"].update(coverage_after_reread="yes"), "true or false"),
        (lambda d: d["opus"].update(complx_amount=1), "unknown settings: complx_amount"),
        (lambda d: d.update(recognized_loss_types=["glass"]), "must include other"),
        (lambda d: d.update(recognized_loss_types=[]), "must be a list"),
        (lambda d: d.update(recognized_loss_types=["other"]), "unknown settings: collision"),
        (lambda d: d["defaults"].update(review_if_reported=["fraud_flag"]), "unknown flags: fraud_flag"),
        (lambda d: d["defaults"].update(active_policy_statuses=[]), "must be a list"),
        (lambda d: d["defaults"].update(prior_claims_window_days=1.5), "whole number"),
        (lambda d: d.update(allow_injection_flags=True), "unknown settings: allow_injection_flags"),
        (lambda d: d["defaults"].update(skip_sender_check=True), "unknown settings: skip_sender_check"),
        (lambda d: d["lines"].update(auto={"max_prior_claim": 1}), "unknown settings: max_prior_claim"),
        (lambda d: d["lines"].update(auto={"required_fields": ["amount_claimed"]}), "must also be in confidence_checked"),
    ],
)
def test_mistakes_in_the_file_are_refused(edit, message):
    with pytest.raises(RulesError, match=message):
        parse_rules(changed(edit))


@pytest.mark.parametrize("content", ["not: [valid", "- a\n- list\n", ""])
def test_unusable_files_are_refused(tmp_path, content):
    path = tmp_path / "rules.yaml"
    path.write_text(content)
    with pytest.raises(RulesError):
        load_rules(path)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(RulesError, match="can't read"):
        load_rules(tmp_path / "nope.yaml")


def test_bad_rules_file_stops_processing_before_any_claim_is_read(settings, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("version: broken\n")
    llm = ScriptedLLM()
    agent = ClaimsAgent(replace(settings, rules_file=path), llm)
    with pytest.raises(RulesError):
        agent.process_email(make_email(tmp_path))
    assert llm.extract_calls == []
    assert agent.claims.by_status("received") == []


def test_default_settings_point_at_the_rules_file():
    assert Settings().rules_file.as_posix() == "config/rules.yaml"


def test_prior_claims_window_can_differ_by_line():
    rules = parse_rules(changed(lambda d: d["lines"].update(property={"max_prior_claims": 0})))
    assert "many_prior_claims" in route(
        rules, policy="PRP-200871", name="Daniel Okafor", sender="d.okafor@example.net", loss_type="water_damage", amount="912.30"
    )[1]
    narrow = parse_rules(changed(lambda d: d["lines"].update(property={"max_prior_claims": 0, "prior_claims_window_days": 30})))
    assert "many_prior_claims" not in route(
        narrow, policy="PRP-200871", name="Daniel Okafor", sender="d.okafor@example.net", loss_type="water_damage", amount="912.30"
    )[1]


def test_active_statuses_come_from_the_file():
    policy = dict(policy="AUT-100411", name="Tomas Lindqvist", sender="tomas.lindqvist@example.com")
    assert "policy_not_active" in route(RULES, **policy)[1]
    rules = parse_rules(changed(lambda d: d["lines"].update(auto={"active_policy_statuses": ["active", "lapsed"]})))
    assert "policy_not_active" not in route(rules, **policy)[1]


def test_injury_review_can_only_be_turned_off_explicitly_and_per_line():
    reported = (field("injury_reported", "true"),)
    assert "injury_reported" in route(RULES, extra=reported)[1]
    rules = parse_rules(changed(lambda d: d["lines"].update(auto={"review_if_reported": ["third_party_involved"]})))
    assert "injury_reported" not in route(rules, extra=reported)[1]
    assert rules.lines["property"].review_if_reported == ("injury_reported", "third_party_involved")


def test_unrecognized_loss_type_is_invalid_and_the_list_comes_from_the_file():
    assert "invalid_value" in route(RULES, loss_type="meteor")[1]
    rules = parse_rules(changed(lambda d: d["recognized_loss_types"].append("meteor")))
    assert "invalid_value" not in route(rules, loss_type="meteor")[1]


def test_prompt_lists_the_loss_types_and_its_version_follows_them():
    assert "exactly one of glass, collision, theft, vandalism, water_damage, fire, storm, flood, other." in extraction_prompt(RULES.recognized_loss_types)
    assert "__LOSS_TYPES__" not in extraction_prompt(RULES.recognized_loss_types)
    assert prompt_version(("glass", "other")) != prompt_version(RULES.recognized_loss_types)


def test_opus_amount_and_reread_trigger_come_from_the_file(settings):
    claim = {"amount": "1500"}
    assert coverage_model_for(claim, False, False, settings, RULES) == settings.model
    assert coverage_model_for(claim, True, False, settings, RULES) == settings.complex_model
    assert coverage_model_for({"amount": "2000"}, False, False, settings, RULES) == settings.complex_model
    assert coverage_model_for({"amount": "2000"}, False, True, settings, RULES) == settings.model
    changed_rules = parse_rules(changed(lambda d: d["opus"].update(complex_amount=1000, coverage_after_reread=False)))
    assert coverage_model_for(claim, False, False, settings, changed_rules) == settings.complex_model
    assert coverage_model_for({"amount": "500"}, True, False, settings, changed_rules) == settings.model


def test_turning_off_the_coverage_redo_sends_an_unsure_check_to_a_person(settings, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(changed(lambda d: d["opus"].update(redo_unsure_coverage=False))))
    llm = ScriptedLLM(coverage=[[submit_call(confidence=0.5)]])
    agent = ClaimsAgent(replace(settings, rules_file=path), llm)
    result = agent.process_email(make_email(tmp_path), received_at=RECEIVED)
    assert llm.coverage_models == [settings.model]
    assert result["status"] == AWAITING_REVIEW
