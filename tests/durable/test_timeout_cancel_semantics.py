"""S2-5: timeout and cancel semantics on the two-step chain.

- timeout: the node fails with node.timeout, the run fails, and a late
  return from the abandoned worker thread must NOT overwrite the terminal
  state;
- cancel: once cancellation lands, no further node starts.
"""

from __future__ import annotations

import time

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, make_backoff, make_plan, make_step


def _two_tool_registry(calls: list, search_sleep_s: float = 0.0):
    from app.control.models import ToolSpec

    registry = ToolRegistry()

    def search(arguments, ctx):
        calls.append("s1-search")
        if search_sleep_s:
            time.sleep(search_sleep_s)
        return ToolResult(status=ToolStatus.SUCCESS, output={"hits": [{"evidence_id": "ev-a"}]},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    def inspect(arguments, ctx):
        calls.append("s2-inspect")
        return ToolResult(status=ToolStatus.SUCCESS, output={"ok": True},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    registry.register(ToolSpec(name="t.search", version="t", risk=Risk.READ_ONLY, description="t",
                               arguments_schema={"type": "object", "properties": {}, "required": []},
                               implemented=True, handler=search))
    registry.register(ToolSpec(name="t.inspect", version="t", risk=Risk.READ_ONLY, description="t",
                               arguments_schema={
                                   "type": "object",
                                   "properties": {"evidence_ids": {"type": "array", "items": {"type": "string"}}},
                                   "required": ["evidence_ids"],
                               },
                               implemented=True, handler=inspect))
    return registry


def _runner(stores: Stores, registry) -> DurableRunner:
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger, backoff=make_backoff(stores.clock),
    )


def _plan(search_timeout_ms: int):
    return make_plan(
        [
            make_step("s1-search", "t.search", timeout_ms=search_timeout_ms),
            make_step("s2-inspect", "t.inspect", depends_on=["s1-search"],
                      arguments={"evidence_ids": ["ev-a"]}),
        ]
    )


def test_timeout_state_consistent_and_late_return_cannot_overwrite(stores) -> None:
    calls: list = []
    plan = _plan(search_timeout_ms=200)
    report = _runner(stores, _two_tool_registry(calls, search_sleep_s=1.0)).run(plan)

    assert report.status == RunState.FAILED
    assert report.reason == "node.timeout"
    assert calls == ["s1-search"]  # s2 never started
    run_id = str(plan.run_id)
    assert stores.checkpoints.get_run(run_id).state == RunState.FAILED
    cps = stores.checkpoints.load_checkpoints(run_id)
    assert cps["s1-search"].state == StepState.FAILED
    assert "s2-inspect" not in cps

    # the abandoned worker thread returns ~1s later; give it time and prove
    # the terminal state is untouched
    time.sleep(1.2)
    assert stores.checkpoints.get_run(run_id).state == RunState.FAILED
    assert stores.checkpoints.load_checkpoints(run_id)["s1-search"].state == StepState.FAILED
    types = [e.type for e in stores.events.stream(run_id)]
    assert types.count(EventType.RUN_FAILED) == 1
    assert EventType.RUN_COMPLETED not in types


def test_cancel_prevents_next_node_from_starting(stores) -> None:
    calls: list = []
    plan = _plan(search_timeout_ms=5_000)
    runner = _runner(stores, _two_tool_registry(calls, search_sleep_s=0.8))
    token = runner.cancel_token(str(plan.run_id))
    token.cancel()  # cancel before the run starts executing nodes
    report = runner.run(plan)

    assert report.status == RunState.CANCELLED
    assert calls == []  # no node ever invoked the handler
    types = [e.type for e in stores.events.stream(str(plan.run_id))]
    assert EventType.NODE_STARTED not in types
    assert EventType.RUN_CANCELLED in types
