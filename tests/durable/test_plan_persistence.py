"""S2-2: the run's ExecutionPlan and workspace are persisted in the durable
store, so a restart (new store instances over the same SQLite file) can resume
WITHOUT the process-in-memory plan map (rt.plans)."""

from __future__ import annotations

import pytest

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry
from app.durable.errors import CheckpointMismatchError
from app.durable.graph import RunState
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    SimulatedCrash,
    Stores,
    make_backoff,
    make_plan,
    make_step,
    reopen_stores,
)

REF = {"ref": "step_output", "from_step": "s1-search", "path": "output.hits[*].evidence_id", "expects": "array<string>"}
SEARCH_OUTPUT = {"hits": [{"evidence_id": "ev-a"}], "mock": False}


def _registry(calls: list, seen_workspaces: list, crash_inspect: bool = False) -> ToolRegistry:
    state = {"crashed": False}

    def search(arguments, ctx):
        calls.append("s1-search")
        seen_workspaces.append(ctx.workspace)
        return ToolResult(status=ToolStatus.SUCCESS, output=SEARCH_OUTPUT,
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    def inspect(arguments, ctx):
        if crash_inspect and not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("kill after search succeeded, before inspect completed")
        calls.append("s2-inspect")
        seen_workspaces.append(ctx.workspace)
        return ToolResult(status=ToolStatus.SUCCESS, output={"mock": False},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    registry = ToolRegistry()
    from app.control.models import ToolSpec

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


def _plan():
    return make_plan(
        [
            make_step("s1-search", "t.search"),
            make_step("s2-inspect", "t.inspect", depends_on=["s1-search"], arguments={"evidence_ids": REF}),
        ]
    )


def test_plan_and_workspace_persisted_on_run(stores) -> None:
    plan = _plan()
    _runner(stores, _registry([], [])).run(plan, workspace="default")
    loaded, workspace = stores.checkpoints.load_plan(str(plan.run_id))
    assert loaded.model_dump_json() == plan.model_dump_json()
    assert workspace == "default"


def test_resume_after_restart_without_inmemory_plan(stores) -> None:
    calls: list = []
    seen: list = []
    plan = _plan()
    runner = _runner(stores, _registry(calls, seen, crash_inspect=True))
    with pytest.raises(SimulatedCrash):
        runner.run(plan, workspace="default")
    assert calls == ["s1-search"]

    # Simulated process restart: brand-new store instances AND no in-memory
    # plan object handed over -- the runner must rebuild everything from SQLite.
    stores2 = reopen_stores(stores)
    report = _runner(stores2, _registry(calls, seen, crash_inspect=False)).resume(
        run_id=str(plan.run_id), force_takeover=True
    )
    assert report.status == RunState.SUCCEEDED
    assert calls == ["s1-search", "s2-inspect"]  # succeeded search checkpoint hit
    assert seen == ["default", "default"]  # workspace restored from the store


def test_resume_without_plan_missing_record_raises(stores) -> None:
    from app.durable.errors import RunNotFoundError

    stores2 = reopen_stores(stores)
    with pytest.raises(RunNotFoundError):
        stores2.checkpoints.load_plan("no-such-run")


def test_fingerprint_mismatch_still_detected(stores) -> None:
    calls: list = []
    plan = _plan()
    _runner(stores, _registry(calls, [])).run(plan, workspace="default")

    tampered = plan.model_copy(update={"goal": "tampered goal"})
    stores2 = reopen_stores(stores)
    with pytest.raises(CheckpointMismatchError):
        _runner(stores2, _registry(calls, [])).resume(tampered, force_takeover=True)
