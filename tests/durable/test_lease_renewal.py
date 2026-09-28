"""Lease renewal: a run whose execution outlives lease_ttl_s must not fence
itself. The runner renews the lease at every node boundary and again before
post-invoke commits; a genuine takeover during the window still fences.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry
from app.durable.events import EventType
from app.durable.graph import RunState
from app.durable.runner import DurableRunner

from tests.durable.conftest import FakeClock, Stores, make_backoff, make_plan, make_step


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def advance(self, seconds: float) -> None:
        time.sleep(seconds)


@pytest.fixture()
def real_stores(tmp_path) -> Stores:
    # lease expiry is a REAL-TIME concern: FakeClock never advances, which
    # would mask the self-fencing bug this file tests
    s = Stores(tmp_path / "durable.db", clock=RealClock())
    yield s
    s.close()


def _slow_registry(calls: list, sleep_s: float):
    from app.control.models import ToolSpec

    registry = ToolRegistry()

    def slow(arguments, ctx):
        calls.append("slow")
        time.sleep(sleep_s)
        return ToolResult(status=ToolStatus.SUCCESS, output={"ok": True},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    registry.register(ToolSpec(name="t.slow", version="t", risk=Risk.READ_ONLY, description="t",
                               arguments_schema={"type": "object", "properties": {}, "required": []},
                               implemented=True, handler=slow))
    return registry


def _runner(stores: Stores, registry, ttl: float) -> DurableRunner:
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger, backoff=make_backoff(stores.clock),
        owner="worker-A", lease_ttl_s=ttl,
    )


def test_long_tool_call_does_not_self_fence(real_stores) -> None:
    # tool runs 3x longer than the lease TTL: without renewal the post-invoke
    # fencing check would kill the run mid-flight and wedge it in RUNNING.
    calls: list = []
    plan = make_plan([make_step("s1", "t.slow", timeout_ms=10_000)])
    report = _runner(real_stores, _slow_registry(calls, 1.2), ttl=0.4).run(plan)
    assert report.status == RunState.SUCCEEDED, f"{report.status} {report.reason}"
    assert report.fenced is False
    assert calls == ["slow"]
    types = [e.type for e in real_stores.events.stream(str(plan.run_id))]
    assert EventType.RUN_COMPLETED in types


def test_two_slow_nodes_renew_between_nodes(real_stores) -> None:
    calls: list = []
    plan = make_plan(
        [make_step("s1", "t.slow", timeout_ms=10_000),
         make_step("s2", "t.slow", depends_on=["s1"], timeout_ms=10_000)]
    )
    report = _runner(real_stores, _slow_registry(calls, 0.8), ttl=0.5).run(plan)
    assert report.status == RunState.SUCCEEDED, f"{report.status} {report.reason}"
    assert calls == ["slow", "slow"]
