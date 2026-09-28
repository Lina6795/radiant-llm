"""RADIANT-Control M3: append-only EventStore.

One immutable row per event, keyed ``(run_id, seq)`` with ``seq`` strictly
increasing per run. The event log is the audit trail and the future SSE
source; mutable state snapshots live separately in
:mod:`app.durable.checkpoint`. ``stream(run_id, after_seq=...)`` is exactly
the read shape an SSE endpoint will replay/tail.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from app.durable._sqlite import ConnectionFactory, StoreBase


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_RETRIED = "node_retried"
    NODE_SKIPPED = "node_skipped"
    WAITING_REVIEW = "waiting_review"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_RESUMED = "run_resumed"
    LEASE_ACQUIRED = "lease_acquired"
    LEASE_TAKEN_OVER = "lease_taken_over"
    BINDING_RESOLVED = "binding_resolved"


@dataclass
class Event:
    run_id: str
    seq: int
    type: EventType
    payload: dict[str, Any]
    created_at: float


class EventStore(StoreBase):
    def __init__(
        self,
        db_path: str,
        *,
        connection_factory: ConnectionFactory | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._now = now or time.time
        super().__init__(db_path, connection_factory=connection_factory)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                """
            )

    def append(self, run_id: str, type: EventType, payload: dict[str, Any] | None = None) -> Event:
        body = json.dumps(payload or {}, sort_keys=True)
        with self._lock:
            for _ in range(5):
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                seq = int(row["next_seq"])
                try:
                    self._conn.execute(
                        "INSERT INTO events (run_id, seq, type, payload_json, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (run_id, seq, type.value, body, self._now()),
                    )
                    return Event(run_id=run_id, seq=seq, type=type, payload=payload or {}, created_at=self._now())
                except Exception:
                    # Lost a seq race against another connection; recompute.
                    continue
        raise RuntimeError(f"could not append event for run {run_id} after retries")

    def stream(self, run_id: str, *, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq"
        params: tuple[Any, ...] = (run_id, after_seq)
        if limit is not None:
            sql += " LIMIT ?"
            params = (run_id, after_seq, limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            Event(
                run_id=row["run_id"],
                seq=row["seq"],
                type=EventType(row["type"]),
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["max_seq"])
