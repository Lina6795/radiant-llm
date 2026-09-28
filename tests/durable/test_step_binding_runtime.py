"""S1-7B: DurableRunner step-output reference resolution (ADR-0003).

Resolution happens before the tool call, reads only succeeded checkpoint
outputs, re-validates resolved arguments against the tool schema, and emits
BINDING_RESOLVED trace events that record shape (count/digest) but never the
resolved values. Empty expansion, missing path segments, and type mismatches
are typed TERMINAL_ERRORs: the target tool handler is never invoked.
"""

from __future__ import annotations

import pytest

from app.control.models import Risk, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry, build_default_registry
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    SimulatedCrash,
    Stores,
    make_backoff,
    make_plan,
    make_step,
    reopen_stores,
)

SEARCH_OUTPUT = {
    "query": "q",
    "hits": [
        {"evidence_id": "ev-a", "page": 1},
        {"evidence_id": "ev-b", "page": 2},
    ],
    "mock": False,
}

REF = {"ref": "step_output", "from_step": "s1-search", "path": "output.hits[*].evidence_id", "expects": "array<string>"}


def _registry(calls: list, received: list, search_output=None, crash_first=False) -> ToolRegistry:
    registry = build_default_registry()
    state = {"crashed": False}

    def search_handler(arguments, ctx):
        calls.append("s1-search")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output=search_output if search_output is not None else SEARCH_OUTPUT,
            metrics=ToolMetrics(latency_ms=1, token_count=0),
            provenance=ctx.provenance,
        )

    def inspect_handler(arguments, ctx):
        if crash_first and not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("simulated process kill inside consumer")
        calls.append("s2-inspect")
        received.append(dict(arguments))
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"inspected": len(arguments.get("evidence_ids") or [])},
            metrics=ToolMetrics(latency_ms=1, token_count=0),
            provenance=ctx.provenance,
        )

    registry.register(
        __import__("app.control.models", fromlist=["ToolSpec"]).ToolSpec(
            name="test.search",
            version="0.0.1-test",
            risk=Risk.READ_ONLY,
            description="test search",
            arguments_schema={"type": "object", "properties": {}, "required": []},
            implemented=True,
            handler=search_handler,
        )
    )
    registry.register(
        __import__("app.control.models", fromlist=["ToolSpec"]).ToolSpec(
            name="test.inspect",
            version="0.0.1-test",
            risk=Risk.READ_ONLY,
            description="test inspect",
            arguments_schema={
                "type": "object",
                "properties": {
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "workspace_id": {"type": "string"},
                },
                "required": ["evidence_ids"],
                "additionalProperties": False,
            },
            implemented=True,
            handler=inspect_handler,
        )
    )
    return registry


def _plan(search_output=None):
    return make_plan(
        [
            make_step("s1-search", "test.search"),
            make_step("s2-inspect", "test.inspect", depends_on=["s1-search"], arguments={"evidence_ids": REF}),
        ]
    )


def _runner(stores: Stores, registry) -> DurableRunner:
    return DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        backoff=make_backoff(stores.clock),
    )


def test_binding_resolves_from_checkpoint(stores) -> None:
    calls: list = []
    received: list = []
    plan = _plan()
    report = _runner(stores, _registry(calls, received)).run(plan)
    assert report.status == RunState.SUCCEEDED
    assert received == [{"evidence_ids": ["ev-a", "ev-b"]}], received

    binding_events = [
        e for e in stores.events.stream(str(plan.run_id)) if e.type == EventType.BINDING_RESOLVED
    ]
    assert len(binding_events) == 1
    payload = binding_events[0].payload
    assert payload["from_step"] == "s1-search"
    assert payload["path"] == "output.hits[*].evidence_id"
    assert payload["expects"] == "array<string>"
    assert payload["count"] == 2
    assert payload["digest"]
    assert "ev-a" not in str(payload) and "ev-b" not in str(payload)

    started = [e for e in stores.events.stream(str(plan.run_id)) if e.type == EventType.NODE_STARTED]
    assert started[1].payload["resolved_arg_keys"] == ["evidence_ids"]


def test_empty_expansion_fails_without_invoking_tool(stores) -> None:
    calls: list = []
    received: list = []
    plan = _plan()
    report = _runner(stores, _registry(calls, received, search_output={"hits": [], "mock": False})).run(plan)
    assert report.status == RunState.FAILED
    assert report.reason == "runtime.binding_empty"
    assert "s2-inspect" not in calls  # handler never invoked
    assert received == []


def test_path_missing_is_typed_error(stores) -> None:
    calls: list = []
    received: list = []
    bad_ref = dict(REF, path="output.hits[*].no_such_key")
    plan = make_plan(
        [
            make_step("s1-search", "test.search"),
            make_step("s2-inspect", "test.inspect", depends_on=["s1-search"], arguments={"evidence_ids": bad_ref}),
        ]
    )
    report = _runner(stores, _registry(calls, received)).run(plan)
    assert report.status == RunState.FAILED
    assert report.reason == "runtime.binding_path_missing"
    assert "s2-inspect" not in calls


def test_type_mismatch_is_typed_error(stores) -> None:
    calls: list = []
    received: list = []
    output = {"hits": [{"evidence_id": 123}], "mock": False}
    plan = _plan()
    report = _runner(stores, _registry(calls, received, search_output=output)).run(plan)
    assert report.status == RunState.FAILED
    assert report.reason == "runtime.binding_type_mismatch"
    assert "s2-inspect" not in calls


def test_crash_resume_resolves_identically(stores) -> None:
    calls: list = []
    received: list = []
    plan = _plan()
    runner = _runner(stores, _registry(calls, received, crash_first=True))
    with pytest.raises(SimulatedCrash):
        runner.run(plan)
    assert calls == ["s1-search"]  # consumer crashed before completing

    stores2 = reopen_stores(stores)
    # The resumed process is a fresh registry/handler (no memory of the crash).
    report = _runner(stores2, _registry(calls, received, crash_first=False)).resume(
        plan, force_takeover=True
    )
    assert report.status == RunState.SUCCEEDED
    assert calls == ["s1-search", "s2-inspect"]  # search checkpoint hit, not re-executed
    assert received == [{"evidence_ids": ["ev-a", "ev-b"]}]
