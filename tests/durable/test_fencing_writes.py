"""S2-6: lease fencing -- after a takeover the stale owner must not write
checkpoints or events, even if its in-flight tool call returns late.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, make_backoff, make_plan, make_step


def _registry(calls: list):
    from app.control.models import ToolSpec

    registry = ToolRegistry()

    def search(arguments, ctx):
        who = "A" if not calls else "B"
        calls.append(who)
        if who == "A":
            time.sleep(0.6)  # A is slow; B's re-execution finishes first
        return ToolResult(status=ToolStatus.SUCCESS, output={"by": who},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    registry.register(ToolSpec(name="t.search", version="t", risk=Risk.READ_ONLY, description="t",
                               arguments_schema={"type": "object", "properties": {}, "required": []},
                               implemented=True, handler=search))
    return registry


def _runner(stores: Stores, registry, owner: str) -> DurableRunner:
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger, backoff=make_backoff(stores.clock),
        owner=owner,
    )


def _plan():
    return make_plan([make_step("s1-search", "t.search")])


def test_stale_owner_cannot_write_after_takeover(stores) -> None:
    calls: list = []
    plan = _plan()
    run_id = str(plan.run_id)

    report_a: dict = {}

    def run_a() -> None:
        report_a["report"] = _runner(stores, _registry(calls), owner="worker-A").run(plan)

    thread_a = threading.Thread(target=run_a)
    thread_a.start()

    # wait until A's node is RUNNING, then take the lease over with B
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cps = stores.checkpoints.load_checkpoints(run_id)
        if cps.get("s1-search") is not None and cps["s1-search"].state == StepState.RUNNING:
            break
        time.sleep(0.01)
    else:
        pytest.fail("node never entered RUNNING")

    report_b = _runner(stores, _registry(calls), owner="worker-B").resume(
        run_id=run_id, owner="worker-B", force_takeover=True
    )
    thread_a.join(timeout=15)

    assert not thread_a.is_alive()
    assert report_b.status == RunState.SUCCEEDED
    assert report_b.results["s1-search"].output == {"by": "B"}

    # A observed the fencing error and stopped writing
    report_a_report = report_a["report"]
    assert report_a_report.fenced is True

    # the persisted checkpoint and event stream carry ONLY B's outcome
    cp = stores.checkpoints.load_checkpoints(run_id)["s1-search"]
    assert cp.state == StepState.SUCCEEDED
    assert cp.output == {"by": "B"}
    completed = [e for e in stores.events.stream(run_id) if e.type == EventType.NODE_COMPLETED]
    assert len(completed) == 1
    assert stores.checkpoints.get_run(run_id).state == RunState.SUCCEEDED
