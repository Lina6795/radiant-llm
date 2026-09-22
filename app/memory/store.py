"""RADIANT-Control M6: SQLite persistence for governed memory.

Two tables:

* ``memories`` -- one row per MemoryRecord (active or superseded; superseded
  rows are kept for audit and never overwritten in place except for the
  ``superseded_by`` / ``status`` markers).
* ``memory_audit`` -- append-only audit trail (write / supersede / delete /
  ttl_cleanup) with a full JSON snapshot of the record at action time.

Delete is a true row DELETE plus an audit record; TTL cleanup deletes only
*expired active* records (superseded history is preserved until explicitly
deleted). DB path comes from the constructor, falling back to the
``RADIANT_MEMORY_DB`` env var, then to an in-memory database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app.memory.models import MemoryRecord, RecordStatus

DB_PATH_ENV = "RADIANT_MEMORY_DB"


class MemoryStoreError(Exception):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    namespace TEXT NOT NULL,
    workspace TEXT NOT NULL,
    subject TEXT NOT NULL,
    value TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    sensitivity TEXT NOT NULL,
    write_reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    valid_from REAL,
    valid_to REAL,
    ttl_seconds REAL,
    status TEXT NOT NULL,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_lookup
    ON memories (workspace, namespace, category, subject, status);
CREATE TABLE IF NOT EXISTS memory_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    at REAL NOT NULL,
    snapshot_json TEXT
);
"""


class MemoryStore:
    def __init__(self, db_path: str | None = None, *, now: Callable[[], float] | None = None) -> None:
        if db_path is None:
            db_path = os.environ.get(DB_PATH_ENV, ":memory:")
        self._db_path = db_path
        self._now = now or time.time
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- row mapping ---------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord.model_validate(
            {
                "memory_id": row["memory_id"],
                "category": row["category"],
                "namespace": row["namespace"],
                "workspace": row["workspace"],
                "subject": row["subject"],
                "value": row["value"],
                "provenance": json.loads(row["provenance_json"]),
                "confidence": row["confidence"],
                "sensitivity": row["sensitivity"],
                "write_reason": row["write_reason"],
                "created_at": row["created_at"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "ttl_seconds": row["ttl_seconds"],
                "status": row["status"],
                "superseded_by": row["superseded_by"],
            }
        )

    def _audit(self, memory_id: str, action: str, reason: str, actor: str, snapshot: MemoryRecord | None) -> None:
        self._conn.execute(
            "INSERT INTO memory_audit (memory_id, action, reason, actor, at, snapshot_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                memory_id,
                action,
                reason,
                actor,
                self._now(),
                snapshot.model_dump_json() if snapshot is not None else None,
            ),
        )

    # -- writes ----------------------------------------------------------------

    def put(self, record: MemoryRecord, *, actor: str = "memory.write_gate") -> MemoryRecord:
        """Insert a new record (Write Gate must have allowed it already)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO memories (memory_id, category, namespace, workspace, subject,"
                    " value, provenance_json, confidence, sensitivity, write_reason, created_at,"
                    " valid_from, valid_to, ttl_seconds, status, superseded_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.memory_id,
                        record.category.value,
                        record.namespace,
                        record.workspace,
                        record.subject,
                        record.value,
                        json.dumps(record.provenance.model_dump(), sort_keys=True),
                        record.confidence,
                        record.sensitivity.value,
                        record.write_reason,
                        record.created_at,
                        record.valid_from,
                        record.valid_to,
                        record.ttl_seconds,
                        record.status.value,
                        record.superseded_by,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryStoreError(f"memory already exists: {record.memory_id}") from exc
            self._audit(record.memory_id, "write", record.write_reason, actor, record)
        return record

    def mark_superseded(self, old_id: str, new_id: str, *, reason: str, actor: str = "memory.supersede") -> None:
        """Flag the old record as superseded by the new one. The old row is
        NOT modified in any other way and remains queryable for audit."""
        with self._lock:
            old = self.get(old_id)
            self._conn.execute(
                "UPDATE memories SET status = ?, superseded_by = ? WHERE memory_id = ?",
                (RecordStatus.SUPERSEDED.value, new_id, old_id),
            )
            self._audit(old_id, "supersede", reason, actor, old)

    def delete(self, memory_id: str, *, reason: str, actor: str = "user") -> None:
        """True delete: the row is removed and a full snapshot is kept in the
        audit trail."""
        with self._lock:
            record = self.get(memory_id)
            self._audit(memory_id, "delete", reason, actor, record)
            self._conn.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))

    def cleanup_expired(self, *, now: float | None = None, actor: str = "memory.ttl_cleanup") -> list[str]:
        """Delete expired *active* records (TTL elapsed or valid_to passed).
        Superseded records are historical and are not touched."""
        now = self._now() if now is None else now
        removed: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE status = ?", (RecordStatus.ACTIVE.value,)
            ).fetchall()
            for row in rows:
                record = self._row_to_record(row)
                if record.is_expired(now):
                    self._audit(record.memory_id, "ttl_cleanup", "expired", actor, record)
                    self._conn.execute("DELETE FROM memories WHERE memory_id = ?", (record.memory_id,))
                    removed.append(record.memory_id)
        return removed

    # -- reads -----------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryRecord:
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"memory not found: {memory_id}")
        return self._row_to_record(row)

    def find_active(
        self,
        *,
        workspace: str,
        category: str | None = None,
        subject: str | None = None,
    ) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE workspace = ? AND status = ?"
        params: list = [workspace, RecordStatus.ACTIVE.value]
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if subject is not None:
            sql += " AND subject = ?"
            params.append(subject)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_records(
        self,
        *,
        workspace: Optional[str] = None,
        namespace: Optional[str] = None,
        category: Optional[str] = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        if workspace is not None:
            sql += " AND workspace = ?"
            params.append(workspace)
        if namespace is not None:
            sql += " AND namespace = ?"
            params.append(namespace)
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if not include_superseded:
            sql += " AND status = ?"
            params.append(RecordStatus.ACTIVE.value)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def audit_trail(self, memory_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM memory_audit"
        params: list = []
        if memory_id is not None:
            sql += " WHERE memory_id = ?"
            params.append(memory_id)
        sql += " ORDER BY audit_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "audit_id": r["audit_id"],
                "memory_id": r["memory_id"],
                "action": r["action"],
                "reason": r["reason"],
                "actor": r["actor"],
                "at": r["at"],
                "snapshot_json": r["snapshot_json"],
            }
            for r in rows
        ]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])
