"""Shared helpers for control-plane contract tests (offline, no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.control.budget import BudgetLedger
from app.control.models import Action, Intent, ReasonCode, ResponseContract, RouterDecision
from app.control.scheduler import ControlPlane

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"

TEST_LEDGER_ALLOWANCES = {
    "default": 100_000,
    "readonly": 100_000,
    "lowbudget": 1_000,
    "full": 100_000,
}


def load_cases(filename: str) -> list[dict]:
    path = BENCHMARKS_DIR / filename
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_ids(cases: list[dict]) -> list[str]:
    return [c["case_id"] for c in cases]


def code_matches(actual_codes: list[str], expected: str) -> bool:
    """An expected reason code matches an exact code or a code prefix before ':'."""
    return any(code == expected or code.startswith(expected + ":") for code in actual_codes)


def tool_call_decision() -> RouterDecision:
    """Stub router output that always requests tools at full confidence."""
    return RouterDecision(
        intent=Intent.KNOWLEDGE_QA,
        action=Action.TOOL_CALL,
        confidence=1.0,
        reason_codes=[ReasonCode.ROUTER_KEYWORD_MATCH.value],
        response_contract=ResponseContract(),
    )


def build_plane_for_plan(plan: dict) -> ControlPlane:
    """Control plane whose router/planner are stubs that always propose the
    given raw plan -- proving swapped-in callables still pass guard + policy."""
    return ControlPlane.build(
        router=lambda _goal: tool_call_decision(),
        planner=lambda _decision, _goal: plan,
        ledger=BudgetLedger(allowances=dict(TEST_LEDGER_ALLOWANCES)),
    )


@pytest.fixture()
def fresh_ledger() -> BudgetLedger:
    return BudgetLedger(allowances=dict(TEST_LEDGER_ALLOWANCES))
