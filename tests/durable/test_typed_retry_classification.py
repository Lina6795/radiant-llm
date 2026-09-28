"""S2-4: typed retry -- only retryable_error is ever retried.

Binding resolution failures and denied results are final even when the step
declares RetryPolicy.TRANSIENT_ONLY; every failed node records its attempt
count and error classification in the trace.
"""

from __future__ import annotations

import pytest

from app.control.models import RetryPolicy, Risk, ToolError, ToolMetrics, ToolResult, ToolStatus
from app.control.registry import ToolRegistry
from app.durable.events import EventType
from app.durable.graph import RunState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, make_backoff, make_plan, make_step

REF = {"ref": "step_output", "from_step": "s1-search", "path": "output.hits[*].evidence_id", "expects": "array<string>"}


def _registry(calls: list, search_output, inspect_result=None):
    from app.control.models import ToolSpec

    registry = ToolRegistry()

    def search(arguments, ctx):
        calls.append("s1-search")
        return ToolResult(status=ToolStatus.SUCCESS, output=search_output,
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    def inspect(arguments, ctx):
        calls.append("s2-inspect")
        if inspect_result is not None:
            return inspect_result(ctx)
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


def _plan():
    return make_plan(
        [
            make_step("s1-search", "t.search"),
            make_step("s2-inspect", "t.inspect", depends_on=["s1-search"],
                      arguments={"evidence_ids": REF}, retry_policy=RetryPolicy.TRANSIENT_ONLY),
        ]
    )


def _assert_no_retry(stores, plan, report, calls, code: str, inspect_invoked: bool = False) -> None:
    run_id = str(plan.run_id)
    assert report.status == RunState.FAILED
    assert report.reason == code
    # Binding errors stop before the handler; a DENIED result comes FROM the
    # handler. Either way there is exactly one inspect attempt, no retry.
    expected = ["s1-search", "s2-inspect"] if inspect_invoked else ["s1-search"]
    assert calls == expected
    cp = stores.checkpoints.load_checkpoints(run_id)["s2-inspect"]
    assert cp.attempt == 1  # no retry despite TRANSIENT_ONLY
    events = list(stores.events.stream(run_id))
    assert not any(e.type == EventType.NODE_RETRIED for e in events)
    failed = [e for e in events if e.type == EventType.NODE_FAILED]
    assert failed[0].payload["classification"] == "terminal"
    assert failed[0].payload["attempts"] == 1


def test_binding_empty_not_retried(stores) -> None:
    calls: list = []
    plan = _plan()
    report = _runner(stores, _registry(calls, {"hits": []})).run(plan)
    _assert_no_retry(stores, plan, report, calls, "runtime.binding_empty")


def test_binding_type_mismatch_not_retried(stores) -> None:
    calls: list = []
    plan = _plan()
    report = _runner(stores, _registry(calls, {"hits": [{"evidence_id": 42}]})).run(plan)
    _assert_no_retry(stores, plan, report, calls, "runtime.binding_type_mismatch")


def test_denied_not_retried(stores) -> None:
    calls: list = []
    denied = lambda ctx: ToolResult(  # noqa: E731
        status=ToolStatus.DENIED,
        error=ToolError(code="evidence.workspace_mismatch", message="denied", retryable=False),
        metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance,
    )
    plan = _plan()
    report = _runner(stores, _registry(calls, {"hits": [{"evidence_id": "ev-a"}]}, inspect_result=denied)).run(plan)
    _assert_no_retry(stores, plan, report, calls, "evidence.workspace_mismatch", inspect_invoked=True)


def test_retryable_error_retried_with_recorded_classification(stores) -> None:
    calls: list = []
    attempts = {"n": 0}

    def flaky(ctx):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return ToolResult(
                status=ToolStatus.RETRYABLE_ERROR,
                error=ToolError(code="net.timeout", message="transient", retryable=True),
                metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance,
            )
        return ToolResult(status=ToolStatus.SUCCESS, output={"ok": True},
                          metrics=ToolMetrics(latency_ms=1, token_count=0), provenance=ctx.provenance)

    plan = _plan()
    report = _runner(stores, _registry(calls, {"hits": [{"evidence_id": "ev-a"}]}, inspect_result=flaky)).run(plan)
    assert report.status == RunState.SUCCEEDED
    assert calls == ["s1-search", "s2-inspect", "s2-inspect", "s2-inspect"]
    events = list(stores.events.stream(str(plan.run_id)))
    retried = [e for e in events if e.type == EventType.NODE_RETRIED]
    assert [e.payload["attempt"] for e in retried] == [1, 2]
    cp = stores.checkpoints.load_checkpoints(str(plan.run_id))["s2-inspect"]
    assert cp.attempt == 3
