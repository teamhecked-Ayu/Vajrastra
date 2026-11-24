"""Measured token accounting.

Replaces the retired "10 billion token context" claim. Only usage
reported by the provider is stored as *measured*. Locally computed
counts are stored as *estimated* and are never labeled as exact.
Per-request, per-session and lifetime totals with a configurable
budget warning and hard stop.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TokenRecord:
    session_id: str
    provider: str
    model: str
    input_measured: int | None = None
    output_measured: int | None = None
    input_estimated: int = 0
    output_estimated: int = 0
    request_id: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.request_id:
            self.request_id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def measured_total(self) -> int:
        return (self.input_measured or 0) + (self.output_measured or 0)

    @property
    def estimated_total(self) -> int:
        return self.input_estimated + self.output_estimated


SCHEMA = """
CREATE TABLE IF NOT EXISTS token_records (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_measured INTEGER,
    output_measured INTEGER,
    input_estimated INTEGER NOT NULL DEFAULT 0,
    output_estimated INTEGER NOT NULL DEFAULT 0,
    timestamp REAL NOT NULL
);
"""


class TokenLedger:
    """Per-request, per-session and lifetime token totals."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def record(self, rec: TokenRecord) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO token_records VALUES (?,?,?,?,?,?,?,?,?)",
                (rec.request_id, rec.session_id, rec.provider, rec.model,
                 rec.input_measured, rec.output_measured,
                 rec.input_estimated, rec.output_estimated, rec.timestamp),
            )
            self._conn.commit()
        return rec.request_id

    def totals(self, session_id: str | None = None) -> dict:
        sql = (
            "SELECT COUNT(*) AS requests, "
            "COALESCE(SUM(input_measured),0) AS in_m, "
            "COALESCE(SUM(output_measured),0) AS out_m, "
            "COALESCE(SUM(input_estimated),0) AS in_e, "
            "COALESCE(SUM(output_estimated),0) AS out_e "
            "FROM token_records"
        )
        params: tuple = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        row = self._conn.execute(sql, params).fetchone()
        return {
            "requests": row["requests"],
            "measured_input": row["in_m"],
            "measured_output": row["out_m"],
            "estimated_input": row["in_e"],
            "estimated_output": row["out_e"],
        }

    def check_budget(self, session_id: str, budget_tokens: int,
                     warn_fraction: float = 0.8) -> str:
        """Return 'ok', 'warning' or 'exceeded' for the session spend."""
        t = self.totals(session_id)
        spent = t["measured_input"] + t["measured_output"]
        if spent >= budget_tokens:
            return "exceeded"
        if spent >= budget_tokens * warn_fraction:
            return "warning"
        return "ok"


def estimate_tokens(text: str) -> int:
    """Crude local estimate. The result is an ESTIMATE, not a measurement."""
    if not text:
        return 0
    return max(1, len(text) // 4)
