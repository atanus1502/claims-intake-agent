"""The LangGraph workflow.

    intake ─► extract_document (one per document, in parallel) ─► consolidate
        ─► coverage_agent ◄─► coverage_tools ─► verify_coverage ─► route ─┬─► END (fast-track)
                                                                         └─► human_review ─► apply_review ─► END

State holds plain JSON-safe values and file paths, never document bytes, so checkpoints stay small.
LangGraph reruns a node from the top when it resumes, so human_review does nothing but wait for the decision.
"""
from __future__ import annotations

import json
import operator
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Annotated, Any, Protocol

import pydantic
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from typing_extensions import TypedDict

from claims import claims_system as status
from claims.audit import AuditLog
from claims.claims_system import ClaimsSystem
from claims.config import Settings
from claims.consolidation import consolidate
from claims.coverage import verify_coverage
from claims.escalation import coverage_model_for, merge_readings, second_reading_reason
from claims.llm import SUBMIT_TOOL, ModelFailure
from claims.models import CoverageAssessment, DocumentExtraction
from claims.policies import PolicyStore
from claims.prompts import NUDGE_MESSAGE, prompt_version
from claims.routing import FAST_TRACK, HUMAN_REVIEW, decide
from claims.rules import RuleSet


class LLM(Protocol):
    def extract(self, document: dict[str, Any], received_at: str, model: str) -> tuple[DocumentExtraction, dict[str, Any]]: ...
    def coverage_turn(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]: ...


class ClaimState(TypedDict, total=False):
    claim_id: str
    sender: str
    subject: str
    received_at: str
    documents: list[dict[str, Any]]
    intake_issues: Annotated[list[dict[str, str]], operator.add]
    extractions: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]
    claim: dict[str, Any]
    coverage_messages: list[dict[str, Any]]
    coverage_model: str
    coverage_turns: int
    coverage_next: str
    pending_tool_calls: list[dict[str, Any]]
    assessment: dict[str, Any] | None
    coverage: dict[str, Any]
    decision: dict[str, Any]
    review: dict[str, Any]


@dataclass
class Deps:
    settings: Settings
    llm: LLM
    claims: ClaimsSystem
    policies: PolicyStore
    rules: RuleSet
    audit: AuditLog


REVIEW_OUTCOMES = {
    "approve": status.APPROVED,
    "deny": status.DENIED,
    "request_info": status.INFO_REQUESTED,
}

_ACKNOWLEDGEMENTS = {
    FAST_TRACK: "Your claim has been accepted for fast-track processing. We'll contact you if we need anything else.",
    HUMAN_REVIEW: "A claims handler is reviewing your claim and will contact you with the next steps.",
}


def build_graph(deps: Deps, checkpointer: Any = None) -> Any:
    audit = deps.audit
    current_prompt_version = prompt_version(deps.rules.recognized_loss_types)

    def intake(state: ClaimState) -> dict[str, Any]:
        claim_id = state["claim_id"]
        hashes = [d["sha256"] for d in state["documents"] if d["kind"] != "email"]
        duplicates = deps.claims.register(claim_id, state["sender"], state["subject"], state["received_at"], hashes)
        issues = []
        if duplicates:
            issues.append({"code": "duplicate_document", "detail": f"An attachment already appears on {', '.join(duplicates)}."})
        audit.write(
            claim_id, "intake", sender=state["sender"], received_at=state["received_at"],
            documents=[{k: d[k] for k in ("doc_id", "kind", "filename", "sha256")} for d in state["documents"]],
            issues=[*state.get("intake_issues", []), *issues],
        )
        return {"intake_issues": issues}

    def dispatch_documents(state: ClaimState) -> list[Send]:
        return [
            Send("extract_document", {"claim_id": state["claim_id"], "received_at": state["received_at"], "document": d})
            for d in state["documents"]
        ]

    def extract_document(payload: dict[str, Any]) -> dict[str, Any]:
        document, claim_id = payload["document"], payload["claim_id"]
        doc_id, sha256 = document["doc_id"], document["sha256"]
        settings = deps.settings
        usage: list[dict[str, Any]] = []

        def read(model: str) -> tuple[dict[str, Any] | None, str | None]:
            try:
                result, call_usage = deps.llm.extract(document, payload["received_at"], model)
            except ModelFailure as exc:
                audit.write(claim_id, "extraction_failed", doc_id=doc_id, sha256=sha256, model=model, error=str(exc))
                return None, str(exc)
            reading = result.model_dump(mode="json")
            usage.append(call_usage)
            audit.write(
                claim_id, "document_extracted", doc_id=doc_id, sha256=sha256, requested_model=model,
                prompt_version=current_prompt_version, usage=call_usage, result=reading,
            )
            return reading, None

        reading, error = read(settings.model)
        why = "the first reading failed" if reading is None else second_reading_reason(reading, deps.rules)
        reread = False
        if why and settings.complex_model != settings.model:
            audit.write(claim_id, "document_reread", doc_id=doc_id, reason=why, model=settings.complex_model)
            second, second_error = read(settings.complex_model)
            if second is not None:
                reading, reread = (merge_readings(reading, second, deps.rules) if reading else second), True
            error = error or second_error
        if reading is None:
            return {"extractions": [{"doc_id": doc_id, "error": error}], "usage": usage}
        return {"extractions": [{"doc_id": doc_id, "result": reading, "reread": reread}], "usage": usage}

    def consolidate_node(state: ClaimState) -> dict[str, Any]:
        claim = consolidate(state["extractions"], deps.rules)
        deps.claims.save_details(state["claim_id"], {"claim": claim})
        audit.write(state["claim_id"], "consolidated", claim=claim)
        coverage_model = coverage_model_for(
            claim,
            any_reread=any(e.get("reread") for e in state["extractions"]),
            already_flagged=bool(claim["issues"] or state.get("intake_issues")),
            settings=deps.settings,
            rules=deps.rules,
        )
        details = {name: {k: f[k] for k in ("value", "confidence")} for name, f in claim["fields"].items()}
        opening = (
            f"Claim details, extracted from the claimant's documents:\n<claim_details>\n"
            f"{json.dumps(details, indent=1)}\n</claim_details>\n\nCheck this claim against the policy."
        )
        return {
            "claim": claim,
            "coverage_messages": [{"role": "user", "content": opening}],
            "coverage_model": coverage_model,
            "coverage_turns": 0,
        }

    def coverage_agent(state: ClaimState) -> dict[str, Any]:
        turns = state["coverage_turns"]
        if turns >= deps.settings.max_coverage_turns:
            audit.write(state["claim_id"], "coverage_turn_limit", turns=turns)
            return {"assessment": None, "coverage_next": "verify_coverage"}
        try:
            turn = deps.llm.coverage_turn(state["coverage_messages"], state["coverage_model"])
        except ModelFailure as exc:
            audit.write(state["claim_id"], "coverage_failed", error=str(exc))
            return {"assessment": None, "coverage_next": "verify_coverage"}
        messages = [*state["coverage_messages"], {"role": "assistant", "content": turn["content"]}]
        update = {"coverage_turns": turns + 1, "usage": [turn["usage"]], "pending_tool_calls": turn["tool_calls"]}
        if turn["tool_calls"]:
            return {**update, "coverage_messages": messages, "coverage_next": "coverage_tools"}
        messages.append({"role": "user", "content": NUDGE_MESSAGE})
        return {**update, "coverage_messages": messages, "coverage_next": "coverage_agent"}

    def coverage_tools(state: ClaimState) -> dict[str, Any]:
        results = []
        assessment = None
        for call in state["pending_tool_calls"]:
            audit.write(state["claim_id"], "coverage_tool_call", tool=call["name"], input=call["input"])
            is_error = False
            if call["name"] == SUBMIT_TOOL["name"]:
                try:
                    assessment = CoverageAssessment.model_validate(call["input"]).model_dump()
                    output = {"status": "received"}
                except pydantic.ValidationError as exc:
                    output, is_error = {"error": str(exc)}, True
            else:
                try:
                    output = deps.policies.run_tool(call["name"], call["input"])
                except KeyError as exc:
                    output, is_error = {"error": f"bad tool call: {exc}"}, True
            results.append({
                "type": "tool_result", "tool_use_id": call["id"], "content": json.dumps(output), "is_error": is_error,
            })
        complex_model = deps.settings.complex_model
        if (
            assessment is not None
            and deps.rules.redo_unsure_coverage
            and state["coverage_model"] != complex_model
            and not assessment["exclusion_applies"]  # a person will see this anyway, and a second opinion mustn't clear it
            and assessment["confidence"] < deps.rules.confidence_threshold
        ):
            audit.write(state["claim_id"], "coverage_escalated", model=complex_model, first_assessment=assessment)
            return {
                "coverage_messages": state["coverage_messages"][:1],
                "coverage_model": complex_model,
                "coverage_turns": 0,
                "coverage_next": "coverage_agent",
                "pending_tool_calls": [],
            }
        if assessment is not None:
            return {"assessment": assessment, "coverage_next": "verify_coverage", "pending_tool_calls": []}
        return {
            "coverage_messages": [*state["coverage_messages"], {"role": "user", "content": results}],
            "coverage_next": "coverage_agent",
            "pending_tool_calls": [],
        }

    def verify_coverage_node(state: ClaimState) -> dict[str, Any]:
        assessment = state.get("assessment")
        coverage = verify_coverage(assessment, state["claim"], state["sender"], deps.policies, deps.rules)
        audit.write(
            state["claim_id"], "coverage_verified", prompt_version=current_prompt_version,
            model=state["coverage_model"], assessment=assessment, coverage=coverage,
        )
        return {"coverage": coverage}

    def route(state: ClaimState) -> dict[str, Any]:
        claim_id = state["claim_id"]
        decision = decide(state["claim"], state["coverage"], state.get("intake_issues", []), state["received_at"], deps.rules)
        deps.claims.save_details(claim_id, {"claim": state["claim"], "coverage": state["coverage"], "decision": decision})
        audit.write(claim_id, "routed", **decision)
        deps.claims.set_status(claim_id, status.FAST_TRACK if decision["route"] == FAST_TRACK else status.PENDING_REVIEW)
        _write_acknowledgement(deps.settings, claim_id, state["sender"], decision["route"])
        audit.write(claim_id, "acknowledgement_queued", route=decision["route"])
        return {"decision": decision}

    def after_route(state: ClaimState) -> str:
        return END if state["decision"]["route"] == FAST_TRACK else "human_review"

    def human_review(state: ClaimState) -> dict[str, Any]:
        return {"review": interrupt({"claim_id": state["claim_id"], "reasons": state["decision"]["reasons"]})}

    def apply_review(state: ClaimState) -> dict[str, Any]:
        review = state["review"]
        audit.write(state["claim_id"], "review_decided", **review)
        deps.claims.set_status(state["claim_id"], REVIEW_OUTCOMES[review["decision"]])
        return {}

    graph = StateGraph(ClaimState)
    graph.add_node("intake", intake)
    graph.add_node("extract_document", extract_document)
    graph.add_node("consolidate", consolidate_node)
    graph.add_node("coverage_agent", coverage_agent)
    graph.add_node("coverage_tools", coverage_tools)
    graph.add_node("verify_coverage", verify_coverage_node)
    graph.add_node("route", route)
    graph.add_node("human_review", human_review)
    graph.add_node("apply_review", apply_review)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", dispatch_documents, ["extract_document"])
    graph.add_edge("extract_document", "consolidate")
    graph.add_edge("consolidate", "coverage_agent")
    graph.add_conditional_edges("coverage_agent", lambda s: s["coverage_next"], ["coverage_agent", "coverage_tools", "verify_coverage"])
    graph.add_conditional_edges("coverage_tools", lambda s: s["coverage_next"], ["coverage_agent", "verify_coverage"])
    graph.add_edge("verify_coverage", "route")
    graph.add_conditional_edges("route", after_route, ["human_review", END])
    graph.add_edge("human_review", "apply_review")
    graph.add_edge("apply_review", END)
    return graph.compile(checkpointer=checkpointer)


def _write_acknowledgement(settings: Settings, claim_id: str, sender: str, route_taken: str) -> None:
    """Queues the first response. It gives the reference and next step, and nothing from the claim's contents."""
    message = EmailMessage()
    message["To"] = sender
    message["Subject"] = f"We've received your claim ({claim_id})"
    message.set_content(f"Thank you for contacting us. Your claim reference is {claim_id}.\n\n{_ACKNOWLEDGEMENTS[route_taken]}\n")
    outbox = settings.data_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{claim_id}.eml").write_bytes(message.as_bytes())
