"""Schema Guard unit tests beyond the frozen set."""

from __future__ import annotations

import uuid

from app.control.models import Budgets, ExecutionPlan, PlanStep, RetryPolicy, Risk
from app.control.registry import build_default_registry
from app.control.schema_guard import SchemaGuard
from tests.control.conftest import code_matches


def _plan(steps: list[PlanStep]) -> ExecutionPlan:
    return ExecutionPlan(
        run_id=uuid.uuid4(),
        goal="unit",
        steps=steps,
        budgets=Budgets(max_tokens=4000, max_tool_calls=4, max_wall_time_ms=30000),
    )


def _step(step_id: str, tool: str = "evidence.search", depends_on: list[str] | None = None) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool=tool,
        arguments={"query": "x"} if tool == "evidence.search" else {},
        depends_on=depends_on or [],
        risk=Risk.READ_ONLY,
        timeout_ms=5000,
        retry_policy=RetryPolicy.NONE,
        idempotency_key=None,
    )


def test_duplicate_step_id_rejected() -> None:
    guard = SchemaGuard(registry=build_default_registry())
    result = guard.validate(_plan([_step("s1"), _step("s1")]))
    assert not result.ok
    assert code_matches(result.reason_codes, "guard.duplicate_step_id")


def test_unknown_dependency_rejected() -> None:
    guard = SchemaGuard(registry=build_default_registry())
    result = guard.validate(_plan([_step("s1", depends_on=["ghost"])]))
    assert not result.ok
    assert code_matches(result.reason_codes, "guard.unknown_dependency")


def test_valid_dag_passes() -> None:
    guard = SchemaGuard(registry=build_default_registry())
    result = guard.validate(_plan([_step("s1"), _step("s2", depends_on=["s1"])]))
    assert result.ok, result.reason_codes


def test_bool_is_not_an_integer_argument() -> None:
    guard = SchemaGuard(registry=build_default_registry())
    step = _step("s1")
    step.arguments = {"query": "x", "top_k": True}
    result = guard.validate(_plan([step]))
    assert not result.ok
    assert code_matches(result.reason_codes, "guard.argument_type_mismatch")
