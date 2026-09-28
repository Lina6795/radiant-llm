"""S1-7A: step output reference contract + Schema Guard static validation.

Per docs/adr/0003-step-output-binding.md:
- references are controlled value types embedded in PlanStep.arguments;
- Guard rejects unknown source step, out-of-closure reference, invalid path
  syntax, and statically impossible type match -- all with typed reason codes;
- plain literal arguments keep passing unchanged (backward compatible).
"""

from __future__ import annotations

import uuid

import pytest

from app.control.models import ExecutionPlan
from app.control.schema_guard import SchemaGuard
from app.control.registry import build_default_registry


@pytest.fixture()
def registry(monkeypatch):
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    monkeypatch.setenv("RADIANT_EVIDENCE_INSPECT_REAL", "1")
    return build_default_registry()


@pytest.fixture()
def guard(registry):
    return SchemaGuard(registry=registry)


def _ref(from_step: str, path: str, expects: str = "array<string>") -> dict:
    return {"ref": "step_output", "from_step": from_step, "path": path, "expects": expects}


def _step(step_id, tool, arguments=None, depends_on=None, risk="read_only"):
    return {
        "step_id": step_id,
        "tool": tool,
        "arguments": arguments or {},
        "depends_on": depends_on or [],
        "risk": risk,
        "timeout_ms": 5000,
        "retry_policy": "transient_only",
        "idempotency_key": None,
    }


def _plan(steps) -> ExecutionPlan:
    return ExecutionPlan.model_validate(
        {
            "run_id": str(uuid.uuid4()),
            "goal": "g",
            "steps": steps,
            "budgets": {"max_tokens": 8000, "max_tool_calls": 4, "max_wall_time_ms": 30000},
        }
    )


def _two_step(inspect_arguments) -> ExecutionPlan:
    return _plan(
        [
            _step("s1-search", "evidence.search", {"query": "q", "top_k": 3}),
            _step("s2-inspect", "evidence.inspect", inspect_arguments, depends_on=["s1-search"]),
        ]
    )


def test_valid_reference_plan_passes(guard):
    plan = _two_step({"evidence_ids": _ref("s1-search", "output.hits[*].evidence_id")})
    result = guard.validate(plan)
    assert result.ok, result.reason_codes


def test_mixed_literal_and_reference_passes(guard):
    plan = _two_step(
        {
            "evidence_ids": _ref("s1-search", "output.hits[*].evidence_id"),
            "workspace_id": "default",
        }
    )
    result = guard.validate(plan)
    assert result.ok, result.reason_codes


def test_unknown_source_step_rejected(guard):
    plan = _two_step({"evidence_ids": _ref("s9-ghost", "output.hits[*].evidence_id")})
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.binding_unknown_step") for c in result.reason_codes), result.reason_codes


def test_future_step_reference_rejected(guard):
    plan = _plan(
        [
            _step("s1-search", "evidence.search", {"query": "q"}),
            _step(
                "s2-inspect",
                "evidence.inspect",
                {"evidence_ids": _ref("s3-later", "output.hits[*].evidence_id")},
                depends_on=["s1-search"],
            ),
            _step("s3-later", "evidence.search", {"query": "q2"}, depends_on=["s2-inspect"]),
        ]
    )
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.binding_not_in_closure") for c in result.reason_codes), result.reason_codes


def test_non_dependency_parallel_step_rejected(guard):
    plan = _plan(
        [
            _step("s1-search", "evidence.search", {"query": "q"}),
            _step("s2-search", "evidence.search", {"query": "q2"}),
            _step(
                "s3-inspect",
                "evidence.inspect",
                {"evidence_ids": _ref("s2-search", "output.hits[*].evidence_id")},
                depends_on=["s1-search"],
            ),
        ]
    )
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.binding_not_in_closure") for c in result.reason_codes), result.reason_codes


@pytest.mark.parametrize("bad_path", ["input.hits", "output.hits[0].id", "output..x", "output.hits[*]", ""])
def test_invalid_path_syntax_rejected(guard, bad_path):
    plan = _two_step({"evidence_ids": _ref("s1-search", bad_path)})
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.binding_invalid_path") for c in result.reason_codes), result.reason_codes


def test_static_type_mismatch_rejected(guard):
    plan = _two_step(
        {"evidence_ids": _ref("s1-search", "output.hits[*].evidence_id", expects="string")}
    )
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.binding_type_mismatch") for c in result.reason_codes), result.reason_codes


def test_scalar_reference_into_scalar_argument_ok(guard):
    plan = _two_step(
        {
            "evidence_ids": _ref("s1-search", "output.hits[*].evidence_id"),
            "workspace_id": _ref("s1-search", "output.query", expects="string"),
        }
    )
    result = guard.validate(plan)
    assert result.ok, result.reason_codes


def test_plain_literal_plan_still_passes(guard):
    plan = _two_step({"evidence_ids": ["ev-abc"], "workspace_id": "default"})
    result = guard.validate(plan)
    assert result.ok, result.reason_codes


def test_plain_dict_value_is_literal_not_reference(guard):
    # A dict WITHOUT the ref marker is an ordinary literal; evidence.search
    # has additionalProperties=false so it must be rejected as unexpected,
    # NOT misinterpreted as a binding (and definitely not crash the guard).
    plan = _plan([_step("s1-search", "evidence.search", {"query": "q", "top_k": {"ref": "step_output"}})])
    result = guard.validate(plan)
    assert not result.ok
    assert any(c.startswith("guard.argument_type_mismatch") for c in result.reason_codes), result.reason_codes
    assert not any("binding" in c for c in result.reason_codes), result.reason_codes
