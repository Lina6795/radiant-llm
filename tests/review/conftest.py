"""Shared fixtures for human-review closed-loop tests (offline, tmp dirs)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

from app.control.models import (
    Budgets,
    ExecutionPlan,
    PlanStep,
    RetryPolicy,
    Risk,
    ToolMetrics,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from app.control.registry import ToolRegistry
from app.durable.checkpoint import CheckpointStore
from app.durable.events import EventStore
from app.durable.idempotency import PersistentIdempotencyLedger
from app.durable.lease import LeaseManager
from app.durable.retry import ExponentialBackoff
from app.durable.runner import DurableRunner

from app.verification.review import ReviewQueue


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


class Stores:
    def __init__(self, db_path: Path) -> None:
        self.clock = FakeClock()
        self.db_path = str(db_path)
        self.checkpoints = CheckpointStore(self.db_path, now=self.clock.now)
        self.events = EventStore(self.db_path, now=self.clock.now)
        self.leases = LeaseManager(self.db_path, now=self.clock.now)
        self.ledger = PersistentIdempotencyLedger(self.db_path, now=self.clock.now)

    def close(self) -> None:
        for store in (self.checkpoints, self.events, self.leases, self.ledger):
            store.close()


@pytest.fixture()
def stores(tmp_path: Path) -> Stores:
    s = Stores(tmp_path / "durable.db")
    yield s
    s.close()


@pytest.fixture()
def review_queue(tmp_path: Path) -> ReviewQueue:
    q = ReviewQueue(tmp_path / "review.db")
    yield q
    q.close()


def ok_tool(output: dict[str, Any], calls: list[str] | None = None, name: str = "tool"):
    def handler(arguments: dict[str, Any], ctx) -> ToolResult:
        if calls is not None:
            calls.append(name)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=dict(output),
            metrics=ToolMetrics(latency_ms=1, token_count=8),
            provenance=ctx.provenance,
        )

    return handler


def register_tool(registry: ToolRegistry, name: str, handler, risk: Risk = Risk.READ_ONLY) -> None:
    registry.register(
        ToolSpec(
            name=name,
            version="0.0.1-review-test",
            risk=risk,
            description="review closed-loop test tool",
            arguments_schema={"type": "object", "properties": {}, "required": []},
            implemented=True,
            handler=handler,
        )
    )


def make_answer_plan(run_id: uuid.UUID | None = None) -> ExecutionPlan:
    """draft -> finalize, where finalize is the review-gated step."""
    return ExecutionPlan(
        run_id=run_id or uuid.uuid4(),
        goal="answer with human review gate",
        steps=[
            PlanStep(
                step_id="draft",
                tool="answer.draft",
                arguments={},
                depends_on=[],
                risk=Risk.READ_ONLY,
                timeout_ms=5_000,
                retry_policy=RetryPolicy.NONE,
            ),
            PlanStep(
                step_id="finalize",
                tool="answer.finalize",
                arguments={},
                depends_on=["draft"],
                risk=Risk.EXTERNAL,
                timeout_ms=5_000,
                retry_policy=RetryPolicy.NONE,
            ),
        ],
        budgets=Budgets(max_tokens=10_000, max_tool_calls=10, max_wall_time_ms=60_000),
    )


def make_runner(stores: Stores, registry: ToolRegistry, **kwargs) -> DurableRunner:
    kwargs.setdefault("backoff", ExponentialBackoff(max_attempts=2, clock=stores.clock))
    return DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        **kwargs,
    )
