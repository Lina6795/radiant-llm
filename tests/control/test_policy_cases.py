"""Frozen policy contract cases (benchmarks/policy_cases.jsonl).

Each case runs the full pipeline with stub router/planner callables that
propose the case's raw plan, so the frozen set also proves that a swapped-in
planner cannot bypass the Schema Guard or the Policy Engine.
"""

from __future__ import annotations

import pytest

from app.control.models import RunStatus
from tests.control.conftest import build_plane_for_plan, case_ids, code_matches, load_cases

CASES = load_cases("policy_cases.jsonl")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_policy_case(case: dict) -> None:
    plane = build_plane_for_plan(case["input"]["plan"])
    summary = plane.run(case["input"]["goal"], workspace=case["input"]["workspace"])

    expected = case["expected"]
    assert summary.status == RunStatus(expected["status"]), summary.reason_codes
    for code in expected.get("reason_codes_contains", []):
        assert code_matches(summary.reason_codes, code), summary.reason_codes

    # Acceptance gate: unauthorized tools must never execute.
    assert summary.tools_executed == expected["tools_executed"]
    if expected["tools_executed"] == 0:
        assert plane.registry.ledger.effect_count == 0

    # Every reject/review must carry at least one stable reason code.
    if summary.status in (RunStatus.REJECTED, RunStatus.NEEDS_REVIEW):
        assert summary.reason_codes
