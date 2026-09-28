"""S2-1: freeze the legal state sequence of the real S1 two-step plan
(s1-search -> s2-inspect with a step-output binding) and the terminal-run
boundary: a succeeded run can be resumed for inspection but never re-executes.
"""

from __future__ import annotations

import uuid

import pytest

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, make_plan, make_step, reopen_stores

REF = {"ref": "step_output", "from_step": "s1-search", "path": "output.hits[*].evidence_id", "expects": "array<string>"}

SEARCH_OUTPUT = {"hits": [{"evidence_id": "ev-a"}, {"evidence_id": "ev-b"}], "mock": False}


def _registry(calls: list) -> "ToolRegistry":
    from app.control.models import ToolSpec
    from app.control.registry import ToolRegistry

    registry = ToolRegistry()

    def search(arguments, ctx):
        calls.append("s1-search")
        return ToolResult(status=ToolStatus.SUCCESS, output=SEARCH_OUTPUT,
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    def inspect(arguments, ctx):
        calls.append("s2-inspect")
        return ToolResult(status=ToolStatus.SUCCESS,
                          output={"evidence": [{"evidence_id": e} for e in arguments["evidence_ids"]], "mock": False},
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
    from tests.durable.conftest import make_backoff

    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger, backoff=make_backoff(stores.clock),
    )


def _two_step_plan():
    return make_plan(
        [
            make_step("s1-search", "t.search"),
            make_step("s2-inspect", "t.inspect", depends_on=["s1-search"], arguments={"evidence_ids": REF}),
        ],
        run_id=uuid.uuid4(),
    )


def test_two_step_state_sequence_is_frozen(stores) -> None:
    calls: list = []
    plan = _two_step_plan()
    report = _runner(stores, _registry(calls)).run(plan)

    # run-level sequence
    assert report.status == RunState.SUCCEEDED
    run_id = str(plan.run_id)
    record = stores.checkpoints.get_run(run_id)
    assert record.state == RunState.SUCCEEDED
    assert record.config_fingerprint == report.resume.config_fingerprint

    # step-level sequence: both steps succeeded in exactly 1 attempt
    checkpoints = stores.checkpoints.load_checkpoints(run_id)
    assert checkpoints["s1-search"].state == StepState.SUCCEEDED
    assert checkpoints["s2-inspect"].state == StepState.SUCCEEDED
    assert checkpoints["s1-search"].attempt == 1
    assert checkpoints["s2-inspect"].attempt == 1

    # exact event order, including the binding point
    types = [e.type for e in stores.events.stream(run_id)]
    assert types == [
        EventType.RUN_STARTED,
        EventType.LEASE_ACQUIRED,
        EventType.NODE_STARTED,      # s1-search
        EventType.NODE_COMPLETED,    # s1-search
        EventType.NODE_STARTED,      # s2-inspect
        EventType.BINDING_RESOLVED,  # s2-inspect arguments resolved from s1 checkpoint
        EventType.NODE_COMPLETED,    # s2-inspect
        EventType.RUN_COMPLETED,
    ], types
    assert calls == ["s1-search", "s2-inspect"]


def test_resume_of_succeeded_run_is_terminal_no_reexecution(stores) -> None:
    calls: list = []
    plan = _two_step_plan()
    runner = _runner(stores, _registry(calls))
    first = runner.run(plan)
    assert first.status == RunState.SUCCEEDED

    # A brand-new runner over the same SQLite file (simulated process restart)
    # must observe the terminal run and execute nothing.
    stores2 = reopen_stores(stores)
    report = _runner(stores2, _registry(calls)).resume(plan, force_takeover=True)
    assert report.status == RunState.SUCCEEDED
    assert report.reason == "run_already_terminal"
    assert report.tools_executed == 0
    assert report.resume.restored_steps == ["s1-search", "s2-inspect"]
    assert calls == ["s1-search", "s2-inspect"]  # no third tool call
    # event stream unchanged: no extra events appended after run_completed
    types = [e.type for e in stores2.events.stream(str(plan.run_id))]
    assert types.count(EventType.RUN_COMPLETED) == 1
