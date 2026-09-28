"""Scenarios a/b: classified retry -- only retryable_error is retried, with
node-level timeout mapped to retryable_error."""

from __future__ import annotations

import threading
import time

from app.control.models import RetryPolicy, ToolStatus
from app.control.registry import build_default_registry
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.retry import ExponentialBackoff, RealClock
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    FakeClock,
    Stores,
    counting_handler,
    flaky_handler,
    make_backoff,
    make_plan,
    make_step,
    register_test_tool,
    spy_invocations,
    terminal_handler,
)
from tests.durable.scenarios import scenario_terminal_error, scenario_transient_retry


def _runner(stores: Stores, registry, **kwargs) -> DurableRunner:
    kwargs.setdefault("backoff", make_backoff(stores.clock))
    return DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        **kwargs,
    )


def test_scenario_a_transient_retry_then_success(tmp_path) -> None:
    detail = scenario_transient_retry(tmp_path)
    assert detail["run_status"] == "succeeded"
    assert detail["retried"] is True
    assert detail["flaky_calls"] == 2  # first retryable_error, second success
    assert detail["upstream_calls"] == 1  # already-succeeded node never re-ran


def test_retry_events_and_checkpoints(tmp_path) -> None:
    stores = Stores(tmp_path / "retry.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.flaky", flaky_handler(calls, name="test.flaky", failures_before_success=1))
    plan = make_plan([make_step("n", "test.flaky", retry_policy=RetryPolicy.TRANSIENT_ONLY)])
    report = _runner(stores, registry).run(plan)
    run_id = str(plan.run_id)

    assert report.status == RunState.SUCCEEDED
    assert report.attempts["n"] == 2
    types = [e.type.value for e in stores.events.stream(run_id)]
    assert types == [
        "run_started",
        "lease_acquired",
        "node_started",
        "node_retried",
        "node_completed",
        "run_completed",
    ]
    checkpoint = stores.checkpoints.load_checkpoints(run_id)["n"]
    assert checkpoint.state == StepState.SUCCEEDED
    assert checkpoint.attempt == 2
    assert checkpoint.config_fingerprint == report.resume.config_fingerprint
    stores.close()


def test_scenario_b_terminal_error_safe_stop(tmp_path) -> None:
    detail = scenario_terminal_error(tmp_path)
    assert detail["run_status"] == "failed"
    assert detail["llm_calls"] == 1  # terminal_error is never retried
    assert detail["retried"] is False
    assert detail["classification"] == "terminal"


def test_denied_is_never_retried(tmp_path) -> None:
    stores = Stores(tmp_path / "denied.db")
    registry = build_default_registry()
    invocations = spy_invocations(registry)
    # report.export without idempotency_key -> the tool itself denies.
    plan = make_plan(
        [make_step("export", "report.export", arguments={"title": "t", "content": "c"},
                   retry_policy=RetryPolicy.TRANSIENT_ONLY)]
    )
    report = _runner(stores, registry).run(plan)
    assert report.status == RunState.FAILED
    assert report.results["export"].status == ToolStatus.DENIED
    assert len(invocations) == 1
    stores.close()


def test_retry_policy_none_disables_retry(tmp_path) -> None:
    stores = Stores(tmp_path / "none.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.flaky", flaky_handler(calls, name="test.flaky", failures_before_success=5))
    plan = make_plan([make_step("n", "test.flaky", retry_policy=RetryPolicy.NONE)])
    report = _runner(stores, registry).run(plan)
    assert report.status == RunState.FAILED
    assert len(calls) == 1
    stores.close()


def test_attempts_exhausted_stops_retrying(tmp_path) -> None:
    stores = Stores(tmp_path / "exhaust.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.flaky", flaky_handler(calls, name="test.flaky", failures_before_success=99))
    plan = make_plan([make_step("n", "test.flaky", retry_policy=RetryPolicy.TRANSIENT_ONLY)])
    report = _runner(stores, registry, backoff=make_backoff(stores.clock, max_attempts=3)).run(plan)
    assert report.status == RunState.FAILED
    assert report.attempts["n"] == 3
    assert len(calls) == 3
    events = stores.events.stream(str(plan.run_id))
    assert sum(1 for e in events if e.type == EventType.NODE_RETRIED) == 2
    stores.close()


def test_backoff_delays_use_injected_clock() -> None:
    clock = FakeClock()
    backoff = ExponentialBackoff(max_attempts=4, base_delay_ms=100.0, multiplier=2.0, clock=clock)
    start = clock.now()
    backoff.wait(1)
    backoff.wait(2)
    backoff.wait(3)
    import pytest

    assert clock.now() - start == pytest.approx((100 + 200 + 400) / 1000.0)  # virtual, no real sleep


def test_node_timeout_is_retryable_and_bounded(tmp_path) -> None:
    """Deterministic: the node blocks on an Event the test never sets until
    after the run returned, so the tool cannot complete early regardless of
    scheduling load -- the runner can only finish via the timeout path."""
    stores = Stores(tmp_path / "timeout.db")
    registry = build_default_registry()
    calls: list[str] = []
    finished: list[str] = []
    release = threading.Event()

    def slow(arguments, ctx):
        calls.append("slow")
        release.wait(timeout=30.0)  # blocks until the test releases it
        finished.append("slow")
        return counting_handler([], name="slow")(arguments, ctx)

    register_test_tool(registry, "test.slow", slow)
    plan = make_plan([make_step("n", "test.slow", timeout_ms=50, retry_policy=RetryPolicy.NONE)])
    try:
        report = _runner(stores, registry).run(plan)
        assert report.status == RunState.FAILED
        assert report.results["n"].status == ToolStatus.RETRYABLE_ERROR
        assert report.results["n"].error.code == "node.timeout"
        assert finished == []  # runner returned while the node was still hung
    finally:
        release.set()  # let the abandoned daemon thread exit
        stores.close()


def test_real_clock_backoff_classifies() -> None:
    backoff = ExponentialBackoff(clock=RealClock())
    assert backoff.classify(_result(ToolStatus.RETRYABLE_ERROR)) == "retryable"
    assert backoff.classify(_result(ToolStatus.TERMINAL_ERROR)) == "terminal"
    assert backoff.classify(_result(ToolStatus.DENIED)) == "terminal"
    assert backoff.classify(_result(ToolStatus.SUCCESS)) == "success"


def _result(status: ToolStatus):
    from app.control.models import ToolResult

    return ToolResult(status=status)
