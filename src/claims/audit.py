"""Append-only audit trail. Each record carries a hash of the one before, so edits and deletions are detectable.

A failed write stops the claim: an automated decision with no record is worse than a delayed one.
The chain shows tampering only if the attacker can't rewrite the whole file, so ship it to write-once storage in production.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claims.redaction import redact

GENESIS = "0" * 64


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, claim_id: str, event: str, **details: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            last = None
            for line in f:
                last = line
            record = {
                "at": datetime.now(timezone.utc).isoformat(),
                "claim_id": claim_id,
                "event": event,
                "details": _clean(details),
                "prev": json.loads(last)["hash"] if last else GENESIS,
            }
            record["hash"] = _digest(record)
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()

    def read(self, claim_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            records = [json.loads(line) for line in f]
        return [r for r in records if claim_id is None or r["claim_id"] == claim_id]

    def verify(self) -> tuple[bool, str]:
        """Walks the chain. Returns whether it's intact and a line saying what was found."""
        prev = GENESIS
        records = self.read()
        for number, record in enumerate(records, start=1):
            body = {k: v for k, v in record.items() if k != "hash"}
            if record["prev"] != prev or _digest(body) != record["hash"]:
                return False, f"chain breaks at record {number} ({record['event']}, {record['claim_id']})"
            prev = record["hash"]
        return True, f"{len(records)} records, chain intact"
