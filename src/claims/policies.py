"""Read-only policy lookups, and the tools Claude uses to reach them.

The store has no write methods and the tools have no write operations, so Claude has no way to change a policy.
Replace PolicyStore with a client for your policy administration system, using a read-only account.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

SEARCH_TOOL = {
    "name": "search_policies",
    "description": (
        "Find policies by holder name and, if known, postal code. Use this when the claim has no policy number, or "
        "when the number it gives isn't found. Returns summaries, with at most ten matches."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "holder_name": {"type": "string", "description": "Full or partial name of the policyholder."},
            "postal_code": {"type": ["string", "null"], "description": "Postal code, or null if not known."},
        },
        "required": ["holder_name", "postal_code"],
        "additionalProperties": False,
    },
}

GET_POLICY_TOOL = {
    "name": "get_policy",
    "description": "Get a policy's status, dates, coverages, limits, deductibles and exclusion wording by its number.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"policy_number": {"type": "string"}},
        "required": ["policy_number"],
        "additionalProperties": False,
    },
}

PRIOR_CLAIMS_TOOL = {
    "name": "list_prior_claims",
    "description": "List earlier claims on a policy, with dates, types and amounts.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"policy_number": {"type": "string"}},
        "required": ["policy_number"],
        "additionalProperties": False,
    },
}

READ_TOOLS = [SEARCH_TOOL, GET_POLICY_TOOL, PRIOR_CLAIMS_TOOL]


def normalise_name(name: str) -> frozenset[str]:
    return frozenset(re.sub(r"[^\w\s]", "", name).casefold().split())


class PolicyStore:
    def __init__(self, path: Path):
        self._policies = {p["policy_number"].upper(): p for p in yaml.safe_load(path.read_text())["policies"]}

    def get(self, policy_number: str) -> dict[str, Any] | None:
        return self._policies.get(policy_number.strip().upper())

    def search(self, holder_name: str, postal_code: str | None = None) -> list[dict[str, Any]]:
        wanted = normalise_name(holder_name)
        matches = []
        for policy in self._policies.values():
            if not wanted or not wanted <= normalise_name(policy["holder_name"]):
                continue
            if postal_code and policy["postal_code"].replace(" ", "").upper() != postal_code.replace(" ", "").upper():
                continue
            matches.append({k: policy[k] for k in ("policy_number", "holder_name", "postal_code", "line", "status")})
        return matches[:10]

    def run_tool(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if name == "search_policies":
            return {"matches": self.search(tool_input["holder_name"], tool_input.get("postal_code"))}
        policy = self.get(tool_input["policy_number"])
        if policy is None:
            return {"error": f"no policy numbered {tool_input['policy_number']}"}
        if name == "get_policy":
            return {k: str(v) if isinstance(v, date) else v for k, v in policy.items()
                    if k not in ("prior_claims", "contact_email")}
        if name == "list_prior_claims":
            return {"prior_claims": _dates_to_text(policy.get("prior_claims", []))}
        raise KeyError(name)


def _dates_to_text(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: str(v) if isinstance(v, date) else v for k, v in row.items()} for row in rows]


def prior_claims_within(policy: dict[str, Any], loss_date: date, days: int = 365) -> int:
    cutoff = loss_date - timedelta(days=days)
    return sum(1 for row in policy.get("prior_claims", []) if cutoff <= _to_date(row["date"]) <= loss_date)


def _to_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
