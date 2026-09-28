"""RADIANT-Control M3: SQLite CheckpointStore.

Persists two kinds of snapshots:

* run records (one row per run: goal, workspace, run state, owner, config
  fingerprint, cancel flag);
* step checkpoints (one row per ``(run_id, step_id)``, upserted every time a
  node changes state -- on start *and* on completion -- carrying the state,
  output, error, attempt number and the config fingerprint active at write
  time).

State snapshots live here; the append-only event log lives in
:mod:`app.durable.events`. The two are deliberately separate tables (and may
be separate stores) so the event stream stays immutable.

PostgreSQL swap: pass ``connection_factory`` (see :mod:`app.durable._sqlite`)
or a ``postgresql://`` DSN once a backend exists.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.control.models import ExecutionPlan
from app.durable._sqlite import ConnectionFactory, StoreBase
from app.durable.errors import DurableError, RunNotFoundError
from app.durable.graph import RunState, StepState


@dataclass
class RunRecord:
    run_id: str
    goal: str
    workspace: str
    state: RunState
    owner: str
    config_fingerprint: str
    cancel_requested: bool
    created_at: float
    updated_at: float


@dataclass
class Checkpoint:
    run_id: str
    step_id: str
    state: StepState
    attempt: int
    output: dict[str, Any] | None
    artifacts: list[str]
    error: dict[str, Any] | None
    config_fingerprint: str
    created_at: float


class CheckpointStore(StoreBase):
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
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    state TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    payload_json TEXT,
                    error_json TEXT,
                    config_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS run_plans (
                    run_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    # -- run records -------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        *,
        goal: str,
        workspace: str,
        owner: str,
        config_fingerprint: str,
    ) -> RunRecord:
        now = self._now()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO runs (run_id, goal, workspace, state, owner, config_fingerprint,"
                    " cancel_requested, created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
                    (run_id, goal, workspace, RunState.PENDING.value, owner, config_fingerprint, now, now),
                )
            except Exception as exc:
                raise DurableError(f"run already exists (use resume): {run_id}") from exc
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return RunRecord(
            run_id=row["run_id"],
            goal=row["goal"],
            workspace=row["workspace"],
            state=RunState(row["state"]),
            owner=row["owner"],
            config_fingerprint=row["config_fingerprint"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_run_state(self, run_id: str, state: RunState) -> None:
        """Persist a run state. Legality of the transition is the caller's
        job (``app.durable.graph.guard_run_transition``)."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (state.value, self._now(), run_id),
            )

    def request_cancel(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE run_id = ?",
                (self._now(), run_id),
            )

    def cancel_requested(self, run_id: str) -> bool:
        return self.get_run(run_id).cancel_requested

    # -- step checkpoints ---------------------------------------------------

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        payload = None
        if checkpoint.output is not None or checkpoint.artifacts:
            payload = json.dumps(
                {"output": checkpoint.output or {}, "artifacts": list(checkpoint.artifacts)},
                sort_keys=True,
            )
        error_json = json.dumps(checkpoint.error, sort_keys=True) if checkpoint.error else None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE checkpoints SET state=?, attempt=?, payload_json=?, error_json=?,"
                " config_fingerprint=?, created_at=? WHERE run_id=? AND step_id=?",
                (
                    checkpoint.state.value,
                    checkpoint.attempt,
                    payload,
                    error_json,
                    checkpoint.config_fingerprint,
                    checkpoint.created_at,
                    checkpoint.run_id,
                    checkpoint.step_id,
                ),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO checkpoints (run_id, step_id, state, attempt, payload_json,"
                    " error_json, config_fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        checkpoint.run_id,
                        checkpoint.step_id,
                        checkpoint.state.value,
                        checkpoint.attempt,
                        payload,
                        error_json,
                        checkpoint.config_fingerprint,
                        checkpoint.created_at,
                    ),
                )

    def load_checkpoints(self, run_id: str) -> dict[str, Checkpoint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at, step_id",
                (run_id,),
            ).fetchall()
        result: dict[str, Checkpoint] = {}
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            result[row["step_id"]] = Checkpoint(
                run_id=row["run_id"],
                step_id=row["step_id"],
                state=StepState(row["state"]),
                attempt=row["attempt"],
                output=payload.get("output"),
                artifacts=list(payload.get("artifacts", [])),
                error=json.loads(row["error_json"]) if row["error_json"] else None,
                config_fingerprint=row["config_fingerprint"],
                created_at=row["created_at"],
            )
        return result

    # -- persisted plans (S2-2) ----------------------------------------------

    def save_plan(self, run_id: str, plan: "ExecutionPlan", workspace: str) -> None:
        """Persist the full ExecutionPlan + workspace so a restart can resume
        without any process-in-memory state (rt.plans)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_plans (run_id, plan_json, workspace, created_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " plan_json=excluded.plan_json, workspace=excluded.workspace",
                (run_id, plan.model_dump_json(), workspace, self._now()),
            )

    def load_plan(self, run_id: str) -> tuple["ExecutionPlan", str]:
        """Rebuild (plan, workspace) from the durable store. Raises
        RunNotFoundError when neither a run record nor a persisted plan exists."""
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_json, workspace FROM run_plans WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return ExecutionPlan.model_validate_json(row["plan_json"]), row["workspace"]
