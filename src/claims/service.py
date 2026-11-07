"""What the CLI and eval call: takes in an email, resumes unfinished work, and applies a reviewer's decision."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from claims.audit import AuditLog
from claims.claims_system import ClaimsSystem
from claims.config import Settings
from claims.documents import claim_id_for, parse_email
from claims.graph import REVIEW_OUTCOMES, Deps, build_graph
from claims.llm import ClaudeLLM
from claims.policies import PolicyStore

PROCESSED = "processed"
AWAITING_REVIEW = "awaiting_review"
DUPLICATE = "duplicate"
DISABLED = "disabled"
OVER_DAILY_LIMIT = "over_daily_limit"


class ClaimsAgent:
    def __init__(self, settings: Settings, llm: Any = None):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.claims = ClaimsSystem(settings.data_dir / "claims.sqlite")
        self.audit = AuditLog(settings.data_dir / "audit.jsonl")
        self._connection = sqlite3.connect(settings.data_dir / "checkpoints.sqlite", check_same_thread=False)
        self._checkpoints = SqliteSaver(self._connection)
        self._llm = llm
        self._graph = None

    @property
    def graph(self) -> Any:
        """Built on first use, so reviewing claims and reading the audit log don't need an API credential."""
        if self._graph is None:
            deps = Deps(
                settings=self.settings,
                llm=self._llm or ClaudeLLM(self.settings),
                claims=self.claims,
                policies=PolicyStore(self.settings.policy_file),
                audit=self.audit,
            )
            self._graph = build_graph(deps, self._checkpoints)
        return self._graph

    def close(self) -> None:
        self._connection.close()

    def process_email(self, path: Path, received_at: str | None = None) -> dict[str, Any]:
        """Runs one email through the graph. Safe to repeat: a finished claim isn't redone and an interrupted one picks up where it stopped."""
        if self.settings.kill_switch.exists():
            return {"status": DISABLED}

        raw = Path(path).read_bytes()
        claim_id = claim_id_for(raw)
        config = _config(claim_id)
        snapshot = self.graph.get_state(config)
        if snapshot.values:
            if not snapshot.next:
                return {"status": DUPLICATE, "claim_id": claim_id}
            if _is_waiting_for_review(snapshot):
                return {"status": AWAITING_REVIEW, "claim_id": claim_id}
            return self._result(claim_id, self.graph.invoke(None, config))

        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if self.claims.count_since(since) >= self.settings.max_per_day:
            return {"status": OVER_DAILY_LIMIT, "claim_id": claim_id}

        parsed = parse_email(raw, self.settings)
        state = {
            "claim_id": claim_id,
            "sender": parsed.sender,
            "subject": parsed.subject,
            "received_at": received_at or datetime.now(timezone.utc).isoformat(),
            "documents": parsed.documents,
            "intake_issues": parsed.issues,
        }
        return self._result(claim_id, self.graph.invoke(state, config))

    def review(self, claim_id: str, decision: str, reviewer: str, note: str = "") -> dict[str, Any]:
        if decision not in REVIEW_OUTCOMES:
            raise ValueError(f"decision must be one of {sorted(REVIEW_OUTCOMES)}")
        if not reviewer.strip():
            raise ValueError("a reviewer name is required")
        config = _config(claim_id)
        snapshot = self.graph.get_state(config)
        if not _is_waiting_for_review(snapshot):
            raise ValueError(f"{claim_id} isn't waiting for review")
        resume = {"decision": decision, "reviewer": reviewer, "note": note}
        return self._result(claim_id, self.graph.invoke(Command(resume=resume), config))

    def _result(self, claim_id: str, state: dict[str, Any]) -> dict[str, Any]:
        waiting = "__interrupt__" in state
        return {
            "status": AWAITING_REVIEW if waiting else PROCESSED,
            "claim_id": claim_id,
            "state": {k: v for k, v in state.items() if k != "__interrupt__"},
            "claim_status": self.claims.get(claim_id)["status"],
        }


def _config(claim_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": claim_id}}


def _is_waiting_for_review(snapshot: Any) -> bool:
    return "human_review" in snapshot.next and any(task.interrupts for task in snapshot.tasks)
