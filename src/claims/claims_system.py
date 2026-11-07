"""Mock claims system on SQLite. Replace this class with a client for your real one; the graph only uses these methods."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RECEIVED = "received"
FAST_TRACK = "fast_track"
PENDING_REVIEW = "pending_review"
APPROVED = "approved_by_reviewer"
DENIED = "denied_by_reviewer"
INFO_REQUESTED = "info_requested"

# A status can only move along these paths, so a replayed or confused step can't undo a person's decision.
_ALLOWED = {
    RECEIVED: {FAST_TRACK, PENDING_REVIEW},
    PENDING_REVIEW: {APPROVED, DENIED, INFO_REQUESTED},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, sender TEXT, subject TEXT, received_at TEXT,
    details TEXT NOT NULL DEFAULT '{}', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (claim_id TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY (claim_id, sha256));
"""


class ClaimsSystem:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, claim_id: str, sender: str, subject: str, received_at: str, hashes: list[str]) -> list[str]:
        """Creates the claim, safely if it's repeated. Returns claims that already hold any of these attachments."""
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO claims (id, status, sender, subject, received_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (claim_id, RECEIVED, sender, subject, received_at, _now(), _now()),
            )
            duplicates: set[str] = set()
            for sha in hashes:
                rows = db.execute("SELECT claim_id FROM documents WHERE sha256=? AND claim_id<>?", (sha, claim_id))
                duplicates.update(row["claim_id"] for row in rows)
                db.execute("INSERT OR IGNORE INTO documents VALUES (?,?)", (claim_id, sha))
        return sorted(duplicates)

    def save_details(self, claim_id: str, details: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE claims SET details=?, updated_at=? WHERE id=?",
                (json.dumps(details, default=str), _now(), claim_id),
            )

    def set_status(self, claim_id: str, status: str) -> None:
        with self._db() as db:
            row = db.execute("SELECT status FROM claims WHERE id=?", (claim_id,)).fetchone()
            if row is None:
                raise KeyError(claim_id)
            if row["status"] != status and status not in _ALLOWED.get(row["status"], set()):
                raise ValueError(f"{claim_id}: can't move from {row['status']} to {status}")
            db.execute("UPDATE claims SET status=?, updated_at=? WHERE id=?", (status, _now(), claim_id))

    def get(self, claim_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return _row(row) if row else None

    def by_status(self, status: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM claims WHERE status=? ORDER BY received_at", (status,)).fetchall()
        return [_row(row) for row in rows]

    def count_since(self, iso_time: str) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM claims WHERE created_at>=?", (iso_time,)).fetchone()[0]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "details": json.loads(row["details"])}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
