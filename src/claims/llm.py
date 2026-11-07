"""Every call to Claude goes through here. A failed call raises ModelFailure, and the graph sends that claim to a person."""
from __future__ import annotations

from typing import Any

import anthropic
import pydantic

from claims.config import Settings
from claims.documents import content_block
from claims.models import CoverageAssessment, DocumentExtraction
from claims.policies import READ_TOOLS
from claims.prompts import COVERAGE_PROMPT, EXTRACTION_PROMPT

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

SUBMIT_TOOL = {
    "name": "submit_coverage_assessment",
    "description": "Submit your assessment of this claim against the policy. Call this once, as your last step.",
    "strict": True,
    "input_schema": CoverageAssessment.model_json_schema(),
}

# USD per million tokens: input, output, cached read. Cache writes are assumed at 1.25x input. Update when prices change.
MODEL_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
}


class ModelFailure(Exception):
    pass


def estimate_cost_usd(usage: list[dict[str, Any]]) -> float | None:
    """Estimated API cost of a set of calls, or None if any used a model not in MODEL_PRICES."""
    total = 0.0
    for call in usage:
        prices = MODEL_PRICES.get(call["model"])
        if prices is None:
            return None
        input_price, output_price, read_price = prices
        total += (
            call["input_tokens"] * input_price
            + call["cache_write_tokens"] * input_price * 1.25
            + call["cache_read_tokens"] * read_price
            + call["output_tokens"] * output_price
        )
    return round(total / 1_000_000, 4)


def _usage(response: Any) -> dict[str, Any]:
    usage = response.usage
    return {
        "model": response.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_input_tokens or 0,
        "cache_write_tokens": usage.cache_creation_input_tokens or 0,
    }


def echoable_content(content: list[Any]) -> list[dict[str, Any]]:
    """The assistant turn to send back. After a mid-output fallback, the declined model's thinking and tool calls must go."""
    blocks = [block.to_dict() for block in content]
    last_fallback = max((i for i, block in enumerate(blocks) if block["type"] == "fallback"), default=None)
    if last_fallback is None:
        return blocks
    kept = [block for block in blocks[:last_fallback] if block["type"] == "text"]
    return kept + blocks[last_fallback + 1:]


class ClaudeLLM:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.Anthropic()

    def _common(self, system: str, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            "output_config": {"effort": self.settings.effort},
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        }

    def extract(self, document: dict[str, Any], received_at: str, model: str) -> tuple[DocumentExtraction, dict[str, Any]]:
        text = (
            f"Received date: {received_at[:10]}\nFile name, supplied by the sender: {document['filename']}\n"
            "Extract the claim details from this document."
        )
        messages = [{"role": "user", "content": [content_block(document), {"type": "text", "text": text}]}]
        try:
            response = self.client.beta.messages.parse(
                **self._common(EXTRACTION_PROMPT, model), output_format=DocumentExtraction, messages=messages
            )
        except (anthropic.APIError, pydantic.ValidationError, ValueError) as exc:
            raise ModelFailure(f"{type(exc).__name__}: {exc}") from exc
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise ModelFailure(f"extraction stopped with {response.stop_reason}")
        return response.parsed_output, _usage(response)

    def coverage_turn(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        try:
            response = self.client.beta.messages.create(
                **self._common(COVERAGE_PROMPT, model), tools=[*READ_TOOLS, SUBMIT_TOOL], messages=messages
            )
        except anthropic.APIError as exc:
            raise ModelFailure(f"{type(exc).__name__}: {exc}") from exc
        if response.stop_reason not in ("end_turn", "tool_use"):
            raise ModelFailure(f"coverage check stopped with {response.stop_reason}")
        content = echoable_content(response.content)
        return {
            "content": content,
            "tool_calls": [
                {"id": block["id"], "name": block["name"], "input": block["input"]}
                for block in content if block["type"] == "tool_use"
            ],
            "usage": _usage(response),
        }
