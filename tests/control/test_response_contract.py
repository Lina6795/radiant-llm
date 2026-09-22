"""response_contract is decoupled from tool authorization."""

from __future__ import annotations

import uuid

from app.control.budget import BudgetLedger
from app.control.models import (
    Budgets,
    ExecutionPlan,
    PlanStep,
    PolicyVerdict,
    RetryPolicy,
    Risk,
)
from app.control.policy import PolicyEngine
from app.control.registry import build_default_registry


def test_policy_engine_never_sees_response_contract() -> None:
    reg = build_default_registry()
    engine = PolicyEngine(registry=reg, ledger=BudgetLedger(allowances={"default": 100_000}))
    plan = ExecutionPlan(
        run_id=uuid.uuid4(),
        goal="formatting must not change authorization",
        steps=[
            PlanStep(
                step_id="s1",
                tool="evidence.search",
                arguments={"query": "x"},
                depends_on=[],
                risk=Risk.READ_ONLY,
                timeout_ms=5000,
                retry_policy=RetryPolicy.NONE,
                idempotency_key=None,
            )
        ],
        budgets=Budgets(max_tokens=4000, max_tool_calls=4, max_wall_time_ms=30000),
    )
    # ExecutionPlan carries no response_contract field at all; the policy
    # decision is a pure function of (plan, workspace).
    assert "response_contract" not in ExecutionPlan.model_fields
    decision = engine.authorize(plan, "default")
    assert decision.verdict == PolicyVerdict.ALLOW
