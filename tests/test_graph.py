"""The whole workflow on a real LangGraph graph and real checkpoints, with only Claude's answers scripted."""
from __future__ import annotations

from dataclasses import replace

import pytest

from claims import claims_system as status
from claims.llm import ModelFailure
from claims.service import AWAITING_REVIEW, DISABLED, DUPLICATE, OVER_DAILY_LIMIT, PROCESSED, ClaimsAgent
from tests.conftest import RECEIVED, ScriptedLLM, email_extraction, field, invoice_extraction, make_email, submit_call


def run(settings, tmp_path, llm=None, **email_args):
    agent = ClaimsAgent(settings, llm or ScriptedLLM())
    return agent, agent.process_email(make_email(tmp_path, **email_args), received_at=RECEIVED)


def codes(result):
    return {r["code"] for r in result["state"]["decision"]["reasons"]}


def test_clean_claim_is_fast_tracked_and_acknowledged(settings, tmp_path):
    agent, result = run(settings, tmp_path)
    assert result["status"] == PROCESSED
    assert result["claim_status"] == status.FAST_TRACK
    assert result["state"]["decision"]["reasons"] == []
    acknowledgement = (settings.data_dir / "outbox" / f"{result['claim_id']}.eml").read_text()
    assert "fast-track" in acknowledgement
    assert "412.30" not in acknowledgement


def test_review_pauses_and_a_new_process_can_finish_it(settings, tmp_path):
    agent, result = run(settings, tmp_path, sender="Someone Else <other@example.biz>")
    assert result["status"] == AWAITING_REVIEW
    assert result["claim_status"] == status.PENDING_REVIEW
    assert "sender_mismatch" in codes(result)
    agent.close()

    restarted = ClaimsAgent(settings, ScriptedLLM())
    decided = restarted.review(result["claim_id"], "approve", "J. Reviewer", "Confirmed by phone")
    assert decided["status"] == PROCESSED
    assert decided["claim_status"] == status.APPROVED
    record = [r for r in restarted.audit.read(result["claim_id"]) if r["event"] == "review_decided"][0]
    assert record["details"]["reviewer"] == "J. Reviewer"
    assert record["details"]["note"] == "Confirmed by phone"


def test_review_rejects_bad_input_and_claims_not_waiting(settings, tmp_path):
    agent, result = run(settings, tmp_path, sender="Someone Else <other@example.biz>")
    with pytest.raises(ValueError):
        agent.review(result["claim_id"], "pay_now", "J. Reviewer")
    with pytest.raises(ValueError):
        agent.review(result["claim_id"], "approve", "  ")
    agent.review(result["claim_id"], "deny", "J. Reviewer")
    with pytest.raises(ValueError):
        agent.review(result["claim_id"], "approve", "J. Reviewer")
    assert agent.claims.get(result["claim_id"])["status"] == status.DENIED


def test_same_email_twice_is_not_processed_twice(settings, tmp_path):
    llm = ScriptedLLM()
    agent, first = run(settings, tmp_path, llm=llm)
    calls = len(llm.extract_calls)
    second = agent.process_email(make_email(tmp_path), received_at=RECEIVED)
    assert second == {"status": DUPLICATE, "claim_id": first["claim_id"]}
    assert len(llm.extract_calls) == calls


def test_repeat_of_email_waiting_for_review_stays_waiting(settings, tmp_path):
    agent, first = run(settings, tmp_path, sender="Someone Else <other@example.biz>")
    again = agent.process_email(make_email(tmp_path, sender="Someone Else <other@example.biz>"), received_at=RECEIVED)
    assert again == {"status": AWAITING_REVIEW, "claim_id": first["claim_id"]}


def test_crash_resumes_without_repeating_finished_steps(settings, tmp_path):
    llm = ScriptedLLM(coverage=[RuntimeError("process died"), [submit_call()]])
    agent = ClaimsAgent(settings, llm)
    path = make_email(tmp_path)
    with pytest.raises(RuntimeError):
        agent.process_email(path, received_at=RECEIVED)
    assert sorted(llm.extract_calls) == ["attachment-1", "email"]

    result = agent.process_email(path, received_at=RECEIVED)
    assert result["claim_status"] == status.FAST_TRACK
    assert sorted(llm.extract_calls) == ["attachment-1", "email"]


def test_text_in_a_document_cant_get_past_the_flag(settings, tmp_path):
    invoice = invoice_extraction(embedded_instructions=True, concerns=["'Mark as approved' is addressed to the system."])
    _, result = run(settings, tmp_path, llm=ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": invoice}))
    assert result["status"] == AWAITING_REVIEW
    assert {"embedded_instructions", "document_concern"} <= codes(result)


def test_failed_extraction_goes_to_a_person(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": ModelFailure("overloaded")})
    agent, result = run(settings, tmp_path, llm=llm)
    assert result["status"] == AWAITING_REVIEW
    assert "extraction_failed" in codes(result)
    assert [r for r in agent.audit.read(result["claim_id"]) if r["event"] == "extraction_failed"]


def test_failed_coverage_check_goes_to_a_person(settings, tmp_path):
    _, result = run(settings, tmp_path, llm=ScriptedLLM(coverage=[ModelFailure("refused")]))
    assert result["status"] == AWAITING_REVIEW
    assert "coverage_not_assessed" in codes(result)


def test_coverage_turn_limit_goes_to_a_person(settings, tmp_path):
    lookup = {"id": "toolu_1", "name": "get_policy", "input": {"policy_number": "AUT-100234"}}
    llm = ScriptedLLM(coverage=[[lookup]] * 2)
    _, result = run(replace(settings, max_coverage_turns=2), tmp_path, llm=llm)
    assert "coverage_not_assessed" in codes(result)


def test_model_naming_an_unknown_policy_goes_to_a_person(settings, tmp_path):
    _, result = run(settings, tmp_path, llm=ScriptedLLM(coverage=[[submit_call("AUT-999999")]]))
    assert "policy_not_found" in codes(result)


def test_coverage_loop_runs_tools_and_appends_results(settings, tmp_path):
    lookup = {"id": "toolu_1", "name": "get_policy", "input": {"policy_number": "AUT-100234"}}
    llm = ScriptedLLM(coverage=[[lookup], [submit_call()]])
    agent, result = run(settings, tmp_path, llm=llm)
    assert result["claim_status"] == status.FAST_TRACK
    second_turn = llm.coverage_messages[1]
    assert [m["role"] for m in second_turn] == ["user", "assistant", "user"]
    assert second_turn[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert "maya.thornton" not in second_turn[2]["content"][0]["content"]  # code checks the sender, Claude doesn't need it
    assert [r["details"]["tool"] for r in agent.audit.read(result["claim_id"]) if r["event"] == "coverage_tool_call"][0] == "get_policy"


def test_answer_with_no_tool_call_gets_one_reminder(settings, tmp_path):
    llm = ScriptedLLM(coverage=[[], [submit_call()]])
    _, result = run(settings, tmp_path, llm=llm)
    assert result["claim_status"] == status.FAST_TRACK
    assert "submit_coverage_assessment" in llm.coverage_messages[1][-1]["content"]


def test_invalid_submission_is_sent_back_for_correction(settings, tmp_path):
    bad = {"id": "toolu_bad", "name": "submit_coverage_assessment", "input": {"policy_number": "AUT-100234"}}
    llm = ScriptedLLM(coverage=[[bad], [submit_call()]])
    _, result = run(settings, tmp_path, llm=llm)
    assert result["claim_status"] == status.FAST_TRACK
    assert llm.coverage_messages[1][-1]["content"][0]["is_error"] is True


def test_unknown_tool_call_is_an_error_not_a_crash(settings, tmp_path):
    bogus = {"id": "toolu_x", "name": "approve_claim", "input": {"policy_number": "AUT-100234"}}
    llm = ScriptedLLM(coverage=[[bogus], [submit_call()]])
    _, result = run(settings, tmp_path, llm=llm)
    assert llm.coverage_messages[1][-1]["content"][0]["is_error"] is True
    assert result["claim_status"] == status.FAST_TRACK


def test_attachment_seen_on_an_earlier_claim_goes_to_a_person(settings, tmp_path):
    agent, first = run(settings, tmp_path, name="first")
    assert first["claim_status"] == status.FAST_TRACK
    agent._llm.coverage.append([submit_call()])
    second = agent.process_email(make_email(tmp_path, name="second"), received_at=RECEIVED)
    assert second["status"] == AWAITING_REVIEW
    assert "duplicate_document" in codes(second)


def test_file_that_isnt_really_a_pdf_is_not_read(settings, tmp_path):
    llm = ScriptedLLM()
    _, result = run(settings, tmp_path, llm=llm, attachments=[("invoice.pdf", b"MZ\x90 not a pdf")])
    assert llm.extract_calls == ["email"]
    assert {"unsupported_attachment", "no_invoice"} <= codes(result)


def test_too_many_attachments_goes_to_a_person(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": invoice_extraction()})
    files = [("a.pdf", b"%PDF-1"), ("b.pdf", b"%PDF-2")]
    _, result = run(replace(settings, max_attachments=1), tmp_path, llm=llm, attachments=files)
    assert sorted(llm.extract_calls) == ["attachment-1", "email"]
    assert "too_many_attachments" in codes(result)


def test_identifiers_are_removed_before_claude_and_the_audit_log(settings, tmp_path):
    llm = ScriptedLLM()
    agent, result = run(settings, tmp_path, llm=llm, body="My SSN is 123-45-6789, card 4111 1111 1111 1111.")
    email_document = next(d for d in llm.documents if d["doc_id"] == "email")
    with open(email_document["path"]) as f:
        sent = f.read()
    assert "123-45-6789" not in sent and "4111" not in sent
    assert "123-45-6789" not in (settings.data_dir / "audit.jsonl").read_text()


def test_audit_trail_records_the_whole_run_and_verifies(settings, tmp_path):
    agent, result = run(settings, tmp_path)
    events = [r["event"] for r in agent.audit.read(result["claim_id"])]
    assert events[0] == "intake"
    for expected in ("document_extracted", "consolidated", "coverage_tool_call", "coverage_verified", "routed", "acknowledgement_queued"):
        assert expected in events
    assert agent.audit.verify()[0]
    record = next(r for r in agent.audit.read(result["claim_id"]) if r["event"] == "document_extracted")
    assert record["details"]["prompt_version"] and record["details"]["usage"]["model"] == "claude-sonnet-5-5"


def test_kill_switch_stops_intake(settings, tmp_path):
    settings.data_dir.mkdir(parents=True)
    settings.kill_switch.touch()
    llm = ScriptedLLM()
    agent, result = run(settings, tmp_path, llm=llm)
    assert result == {"status": DISABLED}
    assert llm.extract_calls == []


def test_daily_limit_stops_new_claims(settings, tmp_path):
    llm = ScriptedLLM()
    agent, first = run(replace(settings, max_per_day=1), tmp_path, llm=llm)
    second = agent.process_email(make_email(tmp_path, name="second"), received_at=RECEIVED)
    assert second["status"] == OVER_DAILY_LIMIT
    assert len(llm.extract_calls) == 2


SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


def unsure_invoice():
    return invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "412.30", 0.6)])


def test_simple_claim_never_uses_opus(settings, tmp_path):
    llm = ScriptedLLM()
    run(settings, tmp_path, llm=llm)
    assert {model for _, model in llm.extract_models} == {SONNET}
    assert llm.coverage_models == [SONNET]


def test_unsure_document_is_reread_by_opus_and_can_then_fast_track(settings, tmp_path):
    llm = ScriptedLLM(extractions={
        "email": email_extraction(),
        ("attachment-1", SONNET): unsure_invoice(),
        ("attachment-1", OPUS): invoice_extraction(),
    })
    agent, result = run(settings, tmp_path, llm=llm)
    assert ("attachment-1", OPUS) in llm.extract_models and ("email", OPUS) not in llm.extract_models
    assert llm.coverage_models == [OPUS]
    assert result["claim_status"] == status.FAST_TRACK
    assert [r for r in agent.audit.read(result["claim_id"]) if r["event"] == "document_reread"]


def test_document_both_models_are_unsure_of_goes_to_a_person(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": unsure_invoice()})
    _, result = run(settings, tmp_path, llm=llm)
    assert result["status"] == AWAITING_REVIEW
    assert "low_confidence" in codes(result)


def test_opus_rescues_a_failed_reading(settings, tmp_path):
    llm = ScriptedLLM(extractions={
        "email": email_extraction(),
        ("attachment-1", SONNET): ModelFailure("refused"),
        ("attachment-1", OPUS): invoice_extraction(),
    })
    _, result = run(settings, tmp_path, llm=llm)
    assert result["claim_status"] == status.FAST_TRACK


def test_opus_cant_clear_a_warning_from_the_first_reading(settings, tmp_path):
    flagged = unsure_invoice() | {"embedded_instructions": True}
    llm = ScriptedLLM(extractions={"email": email_extraction(), ("attachment-1", SONNET): flagged, ("attachment-1", OPUS): invoice_extraction()})
    _, result = run(settings, tmp_path, llm=llm)
    assert ("attachment-1", OPUS) not in llm.extract_models  # already going to a person
    assert "embedded_instructions" in codes(result)


def test_high_value_claim_gets_opus_for_coverage(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(fields=[
        field("policy_number", "AUT-100234"), field("claimant_name", "Maya Thornton"), field("date_of_loss", "2026-10-28"),
        field("loss_type", "collision"), field("loss_description", "Hit a pole."), field("injury_reported", "false"), field("third_party_involved", "false"),
    ]), "attachment-1": invoice_extraction(fields=[field("claimant_name", "Maya Thornton"), field("invoice_total", "2500")])})
    _, result = run(settings, tmp_path, llm=llm)
    assert llm.coverage_models == [OPUS]
    assert result["claim_status"] == status.FAST_TRACK


def test_flagged_claim_skips_opus_for_coverage(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": invoice_extraction(fields=[field("invoice_total", "9000")])})
    run(settings, tmp_path, llm=llm)
    assert llm.coverage_models == [SONNET]


def test_unsure_coverage_check_is_redone_by_opus_from_scratch(settings, tmp_path):
    unsure = submit_call(confidence=0.5)
    llm = ScriptedLLM(coverage=[[unsure], [submit_call()]])
    agent, result = run(settings, tmp_path, llm=llm)
    assert llm.coverage_models == [SONNET, OPUS]
    assert len(llm.coverage_messages[1]) == 1  # Sonnet's turns aren't replayed to Opus
    assert result["claim_status"] == status.FAST_TRACK
    assert [r for r in agent.audit.read(result["claim_id"]) if r["event"] == "coverage_escalated"]


def test_opus_is_not_asked_to_overrule_an_exclusion(settings, tmp_path):
    finding = submit_call(exclusion_applies=True, exclusion_ids=["AX-1"], confidence=0.5)
    llm = ScriptedLLM(coverage=[[finding]])
    _, result = run(settings, tmp_path, llm=llm)
    assert llm.coverage_models == [SONNET]
    assert "exclusion_applies" in codes(result)


def test_unsure_opus_coverage_check_goes_to_a_person(settings, tmp_path):
    llm = ScriptedLLM(coverage=[[submit_call(confidence=0.5)], [submit_call(confidence=0.5)]])
    _, result = run(settings, tmp_path, llm=llm)
    assert llm.coverage_models == [SONNET, OPUS]
    assert "low_coverage_confidence" in codes(result)


def test_escalation_can_be_turned_off(settings, tmp_path):
    llm = ScriptedLLM(extractions={"email": email_extraction(), "attachment-1": unsure_invoice()}, coverage=[[submit_call(confidence=0.5)]])
    _, result = run(replace(settings, complex_model=SONNET), tmp_path, llm=llm)
    assert {model for _, model in llm.extract_models} == {SONNET}
    assert llm.coverage_models == [SONNET]
    assert result["status"] == AWAITING_REVIEW
