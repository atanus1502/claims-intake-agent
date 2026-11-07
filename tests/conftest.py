from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from claims.config import Settings
from claims.llm import ModelFailure
from claims.models import DocumentExtraction

ROOT = Path(__file__).resolve().parent.parent
PDF_BYTES = b"%PDF-1.7 synthetic"
RECEIVED = "2026-11-02T10:00:00+00:00"


def field(name: str, value: str, confidence: float = 0.97) -> dict[str, Any]:
    return {"name": name, "value": value, "confidence": confidence, "evidence": "quoted text"}


def email_extraction(**overrides: Any) -> dict[str, Any]:
    return {
        "doc_type": "email",
        "readable": True,
        "fields": [
            field("policy_number", "AUT-100234"),
            field("claimant_name", "Maya Thornton"),
            field("date_of_loss", "2026-10-28"),
            field("loss_type", "glass"),
            field("loss_description", "A rock cracked the windshield."),
            field("amount_claimed", "412.30"),
        ],
        "concerns": [],
        "embedded_instructions": False,
        **overrides,
    }


def invoice_extraction(**overrides: Any) -> dict[str, Any]:
    return {
        "doc_type": "invoice",
        "readable": True,
        "fields": [field("claimant_name", "Maya Thornton"), field("invoice_number", "GL-55812"), field("invoice_total", "412.30"), field("invoice_date", "2026-10-29")],
        "concerns": [],
        "embedded_instructions": False,
        **overrides,
    }


def submit_call(policy_number: str | None = "AUT-100234", **overrides: Any) -> dict[str, Any]:
    tool_input = {"policy_number": policy_number, "exclusion_applies": False, "exclusion_ids": [], "rationale": "Covered.", "confidence": 0.95, **overrides}
    return {"id": "toolu_submit", "name": "submit_coverage_assessment", "input": tool_input}


class ScriptedLLM:
    """Plays back set answers, and records what it was asked. A coverage step that's an Exception gets raised."""

    def __init__(self, extractions: dict[str, Any] | None = None, coverage: list[Any] | None = None):
        self.extractions = extractions or {"email": email_extraction(), "attachment-1": invoice_extraction()}
        self.coverage = list(coverage if coverage is not None else [[submit_call()]])
        self.extract_calls: list[str] = []
        self.extract_models: list[tuple[str, str]] = []
        self.coverage_models: list[str] = []
        self.documents: list[dict[str, Any]] = []
        self.coverage_messages: list[list[dict[str, Any]]] = []

    def extract(self, document: dict[str, Any], received_at: str, model: str) -> tuple[DocumentExtraction, dict[str, Any]]:
        self.extract_calls.append(document["doc_id"])
        self.extract_models.append((document["doc_id"], model))
        self.documents.append(document)
        # An answer can be scripted for one model with the key (doc_id, model).
        try:
            answer = self.extractions[(document["doc_id"], model)]
        except KeyError:
            answer = self.extractions[document["doc_id"]]
        if isinstance(answer, Exception):
            raise answer
        return DocumentExtraction.model_validate(answer), _usage(model)

    def coverage_turn(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        self.coverage_messages.append(messages)
        self.coverage_models.append(model)
        step = self.coverage.pop(0)
        if isinstance(step, Exception):
            raise step
        content = [{"type": "tool_use", **call} for call in step]
        return {"content": content or [{"type": "text", "text": "Done."}], "tool_calls": step, "usage": _usage(model)}


def _usage(model: str) -> dict[str, Any]:
    return {"model": model, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0}


def make_email(
    tmp_path: Path,
    name: str = "claim",
    sender: str = "Maya Thornton <maya.thornton@example.com>",
    body: str = "A rock cracked my windshield.",
    attachments: list[tuple[str, bytes]] | None = None,
) -> Path:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Windshield claim"
    message["Message-ID"] = f"<{name}@synthetic.test>"
    message.set_content(body)
    for filename, data in attachments if attachments is not None else [("invoice.pdf", PDF_BYTES)]:
        message.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    path = tmp_path / f"{name}.eml"
    path.write_bytes(message.as_bytes())
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return replace(Settings(), data_dir=tmp_path / "data", policy_file=ROOT / "config" / "mock_policies.yaml")


__all__ = ["ModelFailure"]
