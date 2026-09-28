"""S1-8: Guard / Policy negative contract.

Every malformed or unauthorized plan must be rejected BEFORE any tool handler
runs. Each test drives the full ControlPlane (router/planner stubs force a
tool_call decision) with an invoke-spy registry and asserts both the typed
reason code and that the tool handler was invoked exactly zero times.
"""

from __future__ import annotations

import uuid

import pytest

from app.control.budget import BudgetLedger
from app.control.models import Budgets, ExecutionPlan, PlanStep, RetryPolicy, Risk, RunStatus
from app.control.registry import build_default_registry
from app.control.scheduler import ControlPlane

from tests.control.conftest import build_plane_for_plan, code_matches

TEST_BUDGETS = {"max_tokens": 8000, "max_tool_calls": 4, "max_wall_time_ms": 30000}


def _registry_with_spy(monkeypatch):
    registry = build_default_registry()
    calls: list[str] = []
    original = registry.invoke

    def spy(name, arguments, run_id, workspace):
        calls.append(name)
        return original(name, arguments, run_id=run_id, workspace=workspace)

    monkeypatch.setattr(registry, "invoke", spy)
    return registry, calls


def _step(step_id, tool, arguments=None, depends_on=None, risk=Risk.READ_ONLY, idem=None):
    return {
        "step_id": step_id,
        "tool": tool,
        "arguments": arguments or {},
        "depends_on": depends_on or [],
        "risk": risk.value,
        "timeout_ms": 5000,
        "retry_policy": "transient_only",
        "idempotency_key": idem,
    }


def _plan(steps) -> ExecutionPlan:
    return ExecutionPlan.model_validate(
        {
            "run_id": str(uuid.uuid4()),
            "goal": "g",
            "steps": steps,
            "budgets": dict(TEST_BUDGETS),
        }
    )


def _run(monkeypatch, plan, workspace="default", acls=None):
    registry, calls = _registry_with_spy(monkeypatch)
    plane = ControlPlane.build(
        router=lambda _goal: __import__(
            "tests.control.conftest", fromlist=["tool_call_decision"]
        ).tool_call_decision(),
        planner=lambda _d, _g: plan,
        registry=registry,
        ledger=BudgetLedger(allowances={"default": 100_000, "readonly": 100_000, "lowbudget": 1_000, "full": 100_000}),
        workspace_acls=acls,
    )
    summary = plane.run("g", workspace=workspace)
    return summary, calls


def test_unknown_tool_rejected_zero_invocations(monkeypatch):
    summary, calls = _run(monkeypatch, _plan([_step("s1", "no.such.tool")]))
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "guard.unknown_tool")
    assert calls == []


def test_wrong_argument_type_rejected_zero_invocations(monkeypatch):
    plan = _plan([_step("s1", "evidence.search", {"query": "q", "top_k": "three"})])
    summary, calls = _run(monkeypatch, plan)
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "guard.argument_type_mismatch")
    assert calls == []


def test_extra_argument_rejected_zero_invocations(monkeypatch):
    plan = _plan([_step("s1", "evidence.search", {"query": "q", "smuggled": "x"})])
    summary, calls = _run(monkeypatch, plan)
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "guard.unexpected_argument")
    assert calls == []


def test_binding_out_of_closure_rejected_zero_invocations(monkeypatch):
    # s2 references s1 but does NOT declare the dependency: the binding is
    # outside the dependency closure and must fail closed before any tool runs.
    ref = {"ref": "step_output", "from_step": "s1", "path": "output.hits[*].evidence_id", "expects": "array<string>"}
    plan = _plan(
        [
            _step("s1", "evidence.search", {"query": "q"}),
            _step("s2", "evidence.inspect", {"evidence_ids": ref}, depends_on=[]),
        ]
    )
    summary, calls = _run(monkeypatch, plan)
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "guard.binding_not_in_closure")
    assert calls == []


def test_risk_downgrade_rejected_zero_invocations(monkeypatch):
    # report.export is BOUNDED_WRITE in the registry; claiming read_only is a
    # downgrade attempt and must be rejected before any side effect.
    plan = _plan(
        [
            _step(
                "s1",
                "report.export",
                {"title": "t", "content": "c", "idempotency_key": "k"},
                risk=Risk.READ_ONLY,
            )
        ]
    )
    summary, calls = _run(monkeypatch, plan)
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "guard.risk_mismatch")
    assert calls == []


def test_workspace_acl_denied_zero_invocations(monkeypatch):
    plan = _plan([_step("s1", "evidence.search", {"query": "q"})])
    summary, calls = _run(monkeypatch, plan, workspace="vault", acls={"vault": set()})
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "policy.workspace_denied")
    assert calls == []


def test_budget_exceeded_denied_zero_invocations(monkeypatch):
    plan = _plan([_step("s1", "evidence.search", {"query": "q"})])
    summary, calls = _run(monkeypatch, plan, workspace="lowbudget")
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "policy.budget_exceeded")
    assert calls == []


def test_write_without_idempotency_key_denied_zero_invocations(monkeypatch):
    plan = _plan(
        [_step("s1", "report.export", {"title": "t", "content": "c"}, risk=Risk.BOUNDED_WRITE, idem=None)]
    )
    summary, calls = _run(monkeypatch, plan)
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "policy.missing_idempotency_key")
    assert calls == []
