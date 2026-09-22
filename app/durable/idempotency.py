"""RADIANT-Control M3: persistent idempotency ledger.

Drop-in replacement for the in-memory ``IdempotencyLedger`` in
``app/control/registry.py`` on the durable path: same
``execute_once(key, effect) -> (output, replayed)`` contract, but backed by
SQLite so replays survive process restarts. The first call with a key
performs the side effect once and stores its result; every later call with
the same key replays the stored result *without invoking the effect*, so
``effect_count`` (the real number of side effects) never grows on retries --
including retries after a simulated network outage where invoking the effect
again would raise.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from app.durable._sqlite import ConnectionFactory, StoreBase


class PersistentIdempotencyLedger(StoreBase):
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
                CREATE TABLE IF NOT EXISTS idempotency_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    effect_count INTEGER NOT NULL,
                    run_id TEXT,
                    created_at REAL NOT NULL
                );
                """
            )

    def execute_once(
        self,
        key: str,
        effect: Callable[[], dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM idempotency_ledger WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is not None:
                return json.loads(row["result_json"]), True
            # Serialized by the store lock: exactly one caller performs the
            # side effect per key, even across threads.
            output = effect()
            self._conn.execute(
                "INSERT INTO idempotency_ledger (idempotency_key, result_json, effect_count,"
                " run_id, created_at) VALUES (?,?,?,?,?)",
                (key, json.dumps(output, sort_keys=True), 1, run_id, self._now()),
            )
            return dict(output), False

    @property
    def effect_count(self) -> int:
        """Total real side effects performed (same semantic as the M2 ledger)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(effect_count), 0) AS total FROM idempotency_ledger"
            ).fetchone()
        return int(row["total"])

    def effect_count_for(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT effect_count FROM idempotency_ledger WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return int(row["effect_count"]) if row else 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM idempotency_ledger WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return json.loads(row["result_json"]) if row else None
