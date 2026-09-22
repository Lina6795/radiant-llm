"""Shared helpers for durable-runtime fault-injection tests (offline, tmp dirs)."""

from __future__ import annotations

import json
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
    ToolError,
    ToolMetrics,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from app.control.registry import ToolRegistry, build_default_registry
from app.durable.checkpoint import CheckpointStore
from app.durable.events import EventStore
from app.durable.idempotency import PersistentIdempotencyLedger
from app.durable.lease import LeaseManager
from app.durable.retry import ExponentialBackoff

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"

TEST_TOOL_VERSION = "0.0.1-test"


def load_cases(filename: str) -> list[dict]:
    path = BENCHMARKS_DIR / filename
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_ids(cases: list[dict]) -> list[str]:
    return [c["case_id"] for c in cases]


class FakeClock:
    """Deterministic clock: sleep() just advances virtual time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds

    def advance(self, seconds: float) -> None:
        self._t += seconds


class SimulatedCrash(BaseException):
    """Simulates a process kill: BaseException so it is never converted into
    a ToolResult by the registry's fail-closed Exception handler."""


class Stores:
    """The four durable stores over one SQLite file, sharing a clock."""

    def __init__(self, db_path: Path, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.db_path = str(db_path)
        self.checkpoints = CheckpointStore(self.db_path, now=self.clock.now)
        self.events = EventStore(self.db_path, now=self.clock.now)
        self.leases = LeaseManager(self.db_path, now=self.clock.now)
        self.ledger = PersistentIdempotencyLedger(self.db_path, now=self.clock.now)

    def close(self) -> None:
        for store in (self.checkpoints, self.events, self.leases, self.ledger):
            store.close()


def reopen_stores(stores: Stores) -> Stores:
    """New store instances over the same SQLite file -- simulates a process
    restart (new connections, empty in-memory state) without killing pytest."""
    db_path = Path(stores.db_path)
    clock = stores.clock
    stores.close()
    return Stores(db_path, clock)


@pytest.fixture()
def stores(tmp_path: Path) -> Stores:
    s = Stores(tmp_path / "durable.db")
    yield s
    s.close()


def register_test_tool(
    registry: ToolRegistry,
    name: str,
    handler: Callable[..., ToolResult],
    risk: Risk = Risk.READ_ONLY,
) -> None:
    registry.register(
        ToolSpec(
            name=name,
            version=TEST_TOOL_VERSION,
            risk=risk,
            description="durable-runtime test tool",
            arguments_schema={"type": "object", "properties": {}, "required": []},
            implemented=True,
            handler=handler,
        )
    )


def counting_handler(calls: list[str], *, name: str, output: dict[str, Any] | None = None):
    """Tool handler that records each invocation and succeeds."""

    def handler(arguments: dict[str, Any], ctx) -> ToolResult:
        calls.append(name)
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=output or {"ok": True, "tool": name},
            metrics=ToolMetrics(latency_ms=1, token_count=8),
            provenance=ctx.provenance,
        )

    return handler


def flaky_handler(calls: list[str], *, name: str, failures_before_success: int):
    """Fails with retryable_error for the first N invocations, then succeeds."""

    def handler(arguments: dict[str, Any], ctx) -> ToolResult:
        calls.append(name)
        if len(calls) <= failures_before_success:
            return ToolResult(
                status=ToolStatus.RETRYABLE_ERROR,
                error=ToolError(code="net.timeout", message="simulated transient network timeout", retryable=True),
                metrics=ToolMetrics(latency_ms=1, token_count=0),
                provenance=ctx.provenance,
            )
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"ok": True, "tool": name, "calls": len(calls)},
            metrics=ToolMetrics(latency_ms=1, token_count=8),
            provenance=ctx.provenance,
        )

    return handler


def terminal_handler(calls: list[str], *, name: str, code: str = "llm.fatal"):
    """Always fails with terminal_error."""

    def handler(arguments: dict[str, Any], ctx) -> ToolResult:
        calls.append(name)
        return ToolResult(
            status=ToolStatus.TERMINAL_ERROR,
            error=ToolError(code=code, message="simulated permanent LLM failure", retryable=False),
            metrics=ToolMetrics(latency_ms=1, token_count=0),
            provenance=ctx.provenance,
        )

    return handler


def spy_invocations(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Instance-level invoke wrapper counting every tool call (does not touch
    app/control; plain attribute shadowing on this registry instance)."""
    calls: list[dict[str, Any]] = []
    original = registry.invoke

    def spy(name: str, arguments: dict[str, Any], run_id: str, workspace: str) -> ToolResult:
        calls.append({"tool": name, "run_id": run_id})
        return original(name, arguments, run_id=run_id, workspace=workspace)

    registry.invoke = spy  # type: ignore[method-assign]
    return calls


def make_step(
    step_id: str,
    tool: str,
    *,
    depends_on: list[str] | None = None,
    arguments: dict[str, Any] | None = None,
    retry_policy: RetryPolicy = RetryPolicy.NONE,
    risk: Risk = Risk.READ_ONLY,
    timeout_ms: int = 5_000,
    idempotency_key: str | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=tool,
        arguments=arguments or {},
        depends_on=depends_on or [],
        risk=risk,
        timeout_ms=timeout_ms,
        retry_policy=retry_policy,
        idempotency_key=idempotency_key,
    )


def make_plan(steps: list[PlanStep], *, run_id: uuid.UUID | None = None, goal: str = "durable test") -> ExecutionPlan:
    return ExecutionPlan(
        run_id=run_id or uuid.uuid4(),
        goal=goal,
        steps=steps,
        budgets=Budgets(max_tokens=10_000, max_tool_calls=100, max_wall_time_ms=60_000),
    )


def event_types(stores: Stores, run_id: str) -> list[str]:
    return [e.type.value for e in stores.events.stream(run_id)]


def make_backoff(clock: FakeClock, *, max_attempts: int = 3) -> ExponentialBackoff:
    return ExponentialBackoff(max_attempts=max_attempts, base_delay_ms=50.0, multiplier=2.0, clock=clock)
