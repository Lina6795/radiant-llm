"""RADIANT-Control M3: run-level task leases with fencing tokens.

One lease per run: ``(owner, fencing_token, expires_at)``. The fencing token
increases on every acquire, so after a takeover the previous owner's token is
stale and every one of its submissions is rejected with
:class:`LeaseFencingError` -- this is what guarantees a single writer even
when the old owner is merely partitioned, not dead.

Expiry is evaluated against an injectable clock (offline tests advance a
fake clock instead of sleeping).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from app.durable._sqlite import ConnectionFactory, StoreBase
from app.durable.errors import LeaseConflictError, LeaseFencingError


@dataclass
class Lease:
    run_id: str
    owner: str
    fencing_token: int
    acquired_at: float
    expires_at: float


class LeaseManager(StoreBase):
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
                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    def _row_to_lease(self, row) -> Lease:
        return Lease(
            run_id=row["run_id"],
            owner=row["owner"],
            fencing_token=row["fencing_token"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
        )

    def current(self, run_id: str) -> Lease | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_lease(row) if row else None

    def acquire(self, run_id: str, owner: str, ttl_s: float, *, force: bool = False) -> Lease:
        """Acquire or re-acquire the lease for a run.

        * no lease -> create with fencing_token 1;
        * same owner -> renew with an incremented token;
        * different owner, unexpired, not forced -> LeaseConflictError;
        * different owner, expired (or forced) -> takeover, incremented token.
        """
        now = self._now()
        with self._lock:
            row = self._conn.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                token = 1
                self._conn.execute(
                    "INSERT INTO leases (run_id, owner, fencing_token, acquired_at, expires_at)"
                    " VALUES (?,?,?,?,?)",
                    (run_id, owner, token, now, now + ttl_s),
                )
            else:
                expired = row["expires_at"] <= now
                if row["owner"] != owner and not expired and not force:
                    raise LeaseConflictError(run_id, owner, row["owner"])
                token = row["fencing_token"] + 1
                self._conn.execute(
                    "UPDATE leases SET owner=?, fencing_token=?, acquired_at=?, expires_at=?"
                    " WHERE run_id=?",
                    (owner, token, now, now + ttl_s, run_id),
                )
        return Lease(run_id=run_id, owner=owner, fencing_token=token, acquired_at=now, expires_at=now + ttl_s)

    def validate(self, run_id: str, owner: str, fencing_token: int) -> None:
        """Raise LeaseFencingError unless ``(owner, fencing_token)`` is the
        current, unexpired holder. Called before every commit so a fenced
        worker cannot write after a takeover."""
        now = self._now()
        with self._lock:
            row = self._conn.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise LeaseFencingError(run_id, owner, "no lease exists")
        if row["owner"] != owner or row["fencing_token"] != fencing_token:
            raise LeaseFencingError(
                run_id,
                owner,
                f"stale token {fencing_token}; current holder is {row['owner']} with token {row['fencing_token']}",
            )
        if row["expires_at"] <= now:
            raise LeaseFencingError(run_id, owner, "lease expired")
