"""RADIANT-Control M7: Human Review Queue.

SQLite-backed queue (env ``RADIANT_REVIEW_DB``, explicit path wins). A
review item captures the paused run: run_id, the atomic claims under
dispute, an evidence snapshot, the risk reasons that triggered the gate,
and the serialized M2 ``ExecutionPlan`` so the *original* run can be
resumed through the M3 durable runtime — never a freshly created task.

Decision flow:
    enqueue(run paused in waiting_review)
      -> decide(approve|reject|edit, reviewer_id, rationale)
      -> resume_decided(review_id, runner)
           approve/edit : DurableRunner.resume(plan)  -> run completes
           reject       : cancel flag + resume        -> run ends cancelled

Decisions are immutable once recorded, and every decision stores the
inputs it was derived from so scripted-reviewer decisions are replayable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.control.models import ExecutionPlan
from app.durable.runner import DurableRunReport, DurableRunner


class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_items (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    claims TEXT NOT NULL,
    evidence_snapshot TEXT NOT NULL,
    risk_reasons TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT 'default',
    metadata TEXT NOT NULL DEFAULT '{}',
    decision TEXT,
    decision_inputs TEXT,
    reviewer_id TEXT,
    rationale TEXT,
    edited_answer TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_run ON review_items (run_id);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items (status);
"""


def default_review_db_path() -> Path:
    override = os.getenv("RADIANT_REVIEW_DB")
    if override:
        return Path(override)
    config_dir = os.getenv("RADIANT_LLM_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.cwd()
    return base / "review_queue.db"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewQueue:
    def __init__(self, db_path: Optional[str | os.PathLike[str]] = None) -> None:
        path = Path(db_path) if db_path is not None else default_review_db_path()
        if path.parent and str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        # check_same_thread=False: M3 tool handlers run in worker threads and
        # legitimately read the queue through the tool data path (edit flow).
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    def enqueue(
        self,
        *,
        run_id: str,
        claims: List[Dict[str, Any]],
        evidence_snapshot: List[Dict[str, Any]],
        risk_reasons: List[str],
        plan: ExecutionPlan,
        workspace: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        review_id = f"rv-{uuid.uuid4().hex[:16]}"
        self._conn.execute(
            "INSERT INTO review_items (review_id, run_id, status, claims,"
            " evidence_snapshot, risk_reasons, plan_json, workspace, metadata,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                review_id,
                run_id,
                ReviewStatus.PENDING.value,
                json.dumps(claims),
                json.dumps(evidence_snapshot),
                json.dumps(risk_reasons),
                plan.model_dump_json(),
                workspace,
                json.dumps(metadata or {}),
                _utcnow(),
            ),
        )
        self._conn.commit()
        return review_id

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for field in ("claims", "evidence_snapshot", "risk_reasons", "metadata", "decision_inputs"):
            if item.get(field):
                item[field] = json.loads(item[field])
        return item

    def get(self, review_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def pending(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM review_items WHERE status = ? ORDER BY created_at",
            (ReviewStatus.PENDING.value,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def for_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM review_items WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    def decide(
        self,
        review_id: str,
        *,
        decision: ReviewDecision | str,
        reviewer_id: str,
        rationale: str,
        edited_answer: Optional[str] = None,
        decision_inputs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a review decision. Decisions are immutable: a second
        decide() on the same item raises."""
        decision = ReviewDecision(decision)
        item = self.get(review_id)
        if item is None:
            raise KeyError(f"no review item {review_id}")
        if item["status"] != ReviewStatus.PENDING.value:
            raise ValueError(
                f"review item {review_id} already decided ({item['status']}); decisions are immutable"
            )
        if decision is ReviewDecision.EDIT and not (edited_answer or "").strip():
            raise ValueError("edit decision requires a non-empty edited_answer")

        new_status = {
            ReviewDecision.APPROVE: ReviewStatus.APPROVED,
            ReviewDecision.REJECT: ReviewStatus.REJECTED,
            ReviewDecision.EDIT: ReviewStatus.EDITED,
        }[decision]
        self._conn.execute(
            "UPDATE review_items SET status=?, decision=?, decision_inputs=?,"
            " reviewer_id=?, rationale=?, edited_answer=?, decided_at=?"
            " WHERE review_id=?",
            (
                new_status.value,
                decision.value,
                json.dumps(decision_inputs or {}),
                reviewer_id,
                rationale,
                edited_answer,
                _utcnow(),
                review_id,
            ),
        )
        self._conn.commit()
        return self.get(review_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    def resume_decided(
        self,
        review_id: str,
        runner: DurableRunner,
        *,
        workspace: Optional[str] = None,
    ) -> DurableRunReport:
        """Resume the ORIGINAL run referenced by a decided review item
        through the M3 durable runtime.

        approve/edit -> ``runner.resume(plan)`` (waiting_review -> running,
        completed steps restored from checkpoints, never re-executed).
        reject         -> persisted cancel flag + resume, so the run graph
        itself transitions waiting_review -> running -> cancelled with a
        full event trail.
        """
        item = self.get(review_id)
        if item is None:
            raise KeyError(f"no review item {review_id}")
        if item["status"] == ReviewStatus.PENDING.value:
            raise ValueError(f"review item {review_id} is still pending; decide first")
        plan = ExecutionPlan.model_validate_json(item["plan_json"])
        ws = workspace if workspace is not None else item["workspace"]
        if item["status"] == ReviewStatus.REJECTED.value:
            runner.cancel(str(plan.run_id))
        return runner.resume(plan, workspace=ws)


class ScriptedReviewer:
    """Deterministic stand-in for a human reviewer (benchmark B3 + tests).

    Rule: approve only when the verifier found nothing unsupported AND
    every expected fact is present in the answer; otherwise reject. The
    exact inputs are echoed back so the decision can be replayed and
    compared bit-for-bit.
    """

    def __init__(self, reviewer_id: str = "scripted-reviewer") -> None:
        self.reviewer_id = reviewer_id

    def evaluate(
        self,
        *,
        unsupported_claims: List[str],
        missing_facts: List[str],
    ) -> Dict[str, Any]:
        if not unsupported_claims and not missing_facts:
            return {
                "decision": ReviewDecision.APPROVE.value,
                "rationale": "no unsupported claims; all expected facts present",
            }
        reasons = []
        if unsupported_claims:
            reasons.append(f"unsupported claims: {unsupported_claims}")
        if missing_facts:
            reasons.append(f"missing expected facts: {missing_facts}")
        return {"decision": ReviewDecision.REJECT.value, "rationale": "; ".join(reasons)}

    def apply(self, queue: ReviewQueue, review_id: str, *, expected_facts: List[str], answer: str) -> Dict[str, Any]:
        item = queue.get(review_id)
        if item is None:
            raise KeyError(f"no review item {review_id}")
        unsupported = [
            c.get("claim_id", "") for c in item["claims"] if c.get("status") == "unsupported"
        ]
        missing = [f for f in expected_facts if not _fact_present(f, answer)]
        verdict = self.evaluate(unsupported_claims=unsupported, missing_facts=missing)
        inputs = {
            "unsupported_claims": unsupported,
            "missing_facts": missing,
            "answer_sha1": hashlib.sha1(answer.encode()).hexdigest(),
        }
        return queue.decide(
            review_id,
            decision=verdict["decision"],
            reviewer_id=self.reviewer_id,
            rationale=verdict["rationale"],
            decision_inputs=inputs,
        )

    def replay(self, item: Dict[str, Any]) -> str:
        """Re-derive the decision from the stored decision_inputs; raises on
        any mismatch, so a tampered or corrupted row can never pass silently."""
        stored = item.get("decision_inputs") or {}
        verdict = self.evaluate(
            unsupported_claims=stored.get("unsupported_claims", []),
            missing_facts=stored.get("missing_facts", []),
        )
        if verdict["decision"] != item.get("decision"):
            raise ValueError(
                f"replay mismatch on {item.get('review_id')}: stored "
                f"{item.get('decision')} vs re-derived {verdict['decision']}"
            )
        return verdict["decision"]


def _fact_present(fact: str, answer: str) -> bool:
    """Fact presence tolerant to formatting: 'd_model=512' matches
    'd_model = 512'; '100000 steps' matches '100,000 steps'; 'warmup'
    matches 'warm-up'. Alphanumeric parts match on word boundaries so
    'h=8' cannot be satisfied by the 'h' in 'the' or the '8' in '28.4'."""
    import re

    text = (answer or "").lower().replace(",", "").replace("-", "")
    text = re.sub(r"\s*=\s*", "=", text)
    norm = re.sub(r"\s*=\s*", "=", fact.lower().replace(",", "").replace("-", ""))
    for part in norm.split():
        if not part:
            continue
        if part.isalnum():
            if not re.search(rf"\b{re.escape(part)}\b", text):
                return False
        elif part not in text:
            return False
    return True
