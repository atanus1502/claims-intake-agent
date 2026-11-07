from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from claims.documents import parse_email
from claims.eval import CASES_DIR, grade, run_case, summarise
from claims.llm import ModelFailure
from claims.service import ClaimsAgent
from tests.conftest import ScriptedLLM

CASE_DIRS = sorted(p for p in CASES_DIR.iterdir() if (p / "expected.json").exists())


def test_there_are_cases_for_both_routes():
    routes = [json.loads((p / "expected.json").read_text())["route"] for p in CASE_DIRS]
    assert routes.count("fast_track") >= 4
    assert routes.count("human_review") >= 12


@pytest.mark.parametrize("case", CASE_DIRS, ids=lambda p: p.name)
def test_case_is_well_formed(case: Path, settings):
    parsed = parse_email((case / "email.eml").read_bytes(), settings)
    assert parsed.issues == []
    assert parsed.sender
    assert len(parsed.documents) >= 2
    expected = json.loads((case / "expected.json").read_text())
    assert expected["route"] in ("fast_track", "human_review")
    if expected["route"] == "human_review":
        assert expected["any_reason"]


def _state(route, codes, amount="412.30"):
    return {
        "decision": {"route": route, "reasons": [{"code": c, "detail": ""} for c in codes]},
        "claim": {"amount": amount, "amount_confidence": 0.95, "fields": {"loss_type": {"value": "glass", "confidence": 0.99}}},
    }


def test_grade_counts_a_wrong_fast_track():
    graded = grade({"route": "human_review", "any_reason": ["sender_mismatch"]}, _state("fast_track", []))
    assert graded["wrongly_fast_tracked"] is True
    assert graded["route_correct"] is False
    assert graded["reason_correct"] is False


def test_grade_needs_the_right_reason_not_just_the_right_route():
    expected = {"route": "human_review", "any_reason": ["sender_mismatch"]}
    assert grade(expected, _state("human_review", ["low_confidence"]))["reason_correct"] is False
    assert grade(expected, _state("human_review", ["sender_mismatch"]))["reason_correct"] is True


def test_grade_checks_fields_exactly():
    expected = {"route": "fast_track", "fields": {"amount": "412.3", "loss_type": "GLASS", "policy_number": "AUT-1"}}
    results = {c["field"]: c["correct"] for c in grade(expected, _state("fast_track", []))["fields"]}
    assert results == {"amount": True, "loss_type": True, "policy_number": False}


def test_run_case_and_summary_with_scripted_answers(settings, monkeypatch):
    monkeypatch.setattr("claims.service.ClaudeLLM", lambda s: ScriptedLLM(
        extractions=_AnyDocument(), coverage=[[]] * 8,
    ))
    case = next(p for p in CASE_DIRS if p.name.startswith("FT-01"))
    result = run_case(case, settings)
    assert result["route"] == "human_review"  # the script gives no coverage answer, so it must fail safe
    assert result["wrongly_fast_tracked"] is False
    summary = summarise([result], settings)
    assert summary["fast_track_sent_to_person"] == 1
    assert summary["wrongly_fast_tracked"] == 0


class _AnyDocument(dict):
    def __getitem__(self, key):
        return {"doc_type": "other", "readable": True, "fields": [], "concerns": [], "embedded_instructions": False}


def test_model_outage_is_reported_not_mistaken_for_an_escalation(settings, monkeypatch):
    outage = ModelFailure("credit balance is too low")

    class _Outage(dict):
        def __getitem__(self, key):
            return outage

    monkeypatch.setattr("claims.service.ClaudeLLM", lambda s: ScriptedLLM(extractions=_Outage(), coverage=[outage]))
    case = next(p for p in CASE_DIRS if p.name.startswith("HR-13"))
    result = run_case(case, settings)
    assert result["route"] == "human_review"
    assert "credit balance is too low" in result["model_errors"][0]
    assert summarise([result], settings)["cases_with_model_errors"] == 1


def test_a_case_that_crashes_is_reported_not_lost(settings, monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr(ClaimsAgent, "process_email", boom)
    result = run_case(CASE_DIRS[0], replace(settings))
    assert result["error"] == "RuntimeError: bad"
    assert summarise([result], settings)["errors"] == 1
