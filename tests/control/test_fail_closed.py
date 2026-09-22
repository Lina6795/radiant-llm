"""Fail-closed behavior: planner/router failures never reach the executor."""

from __future__ import annotations

from app.control.budget import BudgetLedger
from app.control.models import RunStatus
from app.control.scheduler import ControlPlane
from tests.control.conftest import TEST_LEDGER_ALLOWANCES, code_matches, tool_call_decision


def _plane(planner) -> ControlPlane:
    return ControlPlane.build(
        router=lambda _goal: tool_call_decision(),
        planner=planner,
        ledger=BudgetLedger(allowances=dict(TEST_LEDGER_ALLOWANCES)),
    )


def test_planner_exception_executes_nothing() -> None:
    def exploding_planner(_decision, _goal):
        raise RuntimeError("planner blew up")

    plane = _plane(exploding_planner)
    summary = plane.run("Search for shielding data")
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "planner.error")
    assert summary.tools_executed == 0
    assert plane.registry.ledger.effect_count == 0


def test_planner_non_json_output_executes_nothing() -> None:
    plane = _plane(lambda _decision, _goal: "this is not a plan")
    summary = plane.run("Search for shielding data")
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "planner.invalid_output")
    assert summary.tools_executed == 0


def test_planner_malformed_plan_executes_nothing() -> None:
    plane = _plane(lambda _decision, _goal: {"steps": "garbage", "budgets": {}})
    summary = plane.run("Search for shielding data")
    assert summary.status == RunStatus.REJECTED
    assert code_matches(summary.reason_codes, "planner.invalid_output")
    assert summary.tools_executed == 0


def test_router_exception_fails_closed_to_clarify() -> None:
    def exploding_router(_goal):
        raise ValueError("router blew up")

    plane = ControlPlane.build(router=exploding_router)
    summary = plane.run("anything")
    assert summary.status == RunStatus.CLARIFIED
    assert code_matches(summary.reason_codes, "router.error")
    assert summary.tools_executed == 0
