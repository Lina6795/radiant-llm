"""Idempotency: report.export repeats with the same key cause no second side effect."""

from __future__ import annotations

import uuid

from app.control.models import RunStatus, ToolStatus
from tests.control.conftest import build_plane_for_plan


def _export_plan(key: str) -> dict:
    return {
        "run_id": str(uuid.uuid4()),
        "goal": "export a report",
        "steps": [
            {
                "step_id": "s1",
                "tool": "report.export",
                "arguments": {"title": "t", "content": "c", "idempotency_key": key},
                "depends_on": [],
                "risk": "bounded_write",
                "timeout_ms": 5000,
                "retry_policy": "none",
                "idempotency_key": key,
            }
        ],
        "budgets": {"max_tokens": 4000, "max_tool_calls": 4, "max_wall_time_ms": 30000},
    }


def test_same_idempotency_key_replays_without_second_side_effect() -> None:
    key = "idem-key-1"
    plane = build_plane_for_plan(_export_plan(key))

    first = plane.run("export a report", workspace="default")
    assert first.status == RunStatus.COMPLETED and first.tools_executed == 1
    assert plane.registry.ledger.effect_count == 1

    # Same key, retried through the same plane (shared ledger).
    second = plane.run("export a report", workspace="default")
    assert second.status == RunStatus.COMPLETED and second.tools_executed == 1
    assert plane.registry.ledger.effect_count == 1  # no second side effect
    assert second.results[0].status == ToolStatus.SUCCESS
    assert second.results[0].output["idempotent_replay"] is True
    assert first.results[0].artifacts == second.results[0].artifacts

    # A different key is a new side effect (same shared registry/ledger).
    from app.control.budget import BudgetLedger
    from app.control.scheduler import ControlPlane
    from tests.control.conftest import TEST_LEDGER_ALLOWANCES, tool_call_decision

    other = ControlPlane.build(
        router=lambda _goal: tool_call_decision(),
        planner=lambda _decision, _goal: _export_plan("idem-key-2"),
        registry=plane.registry,
        ledger=BudgetLedger(allowances=dict(TEST_LEDGER_ALLOWANCES)),
    )
    third = other.run("export a report", workspace="default")
    assert third.status == RunStatus.COMPLETED
    assert plane.registry.ledger.effect_count == 2
