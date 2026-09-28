"""RADIANT-Control M2: Schema Guard.

Validates an ExecutionPlan against the tool registry before the Policy Engine
sees it:
- every tool name must be registered;
- arguments must satisfy the tool's JSON schema (required keys, types, no
  unexpected/extra fields -- extra fields are treated as potentially
  sensitive smuggled parameters);
- depends_on must reference existing steps and form an acyclic DAG;
- budgets must be positive and within hard caps, and max_tool_calls must
  cover the declared steps;
- the declared per-step risk must match the registry risk (a plan may not
  downgrade its own risk).

The runtime has no `jsonschema` package, so a minimal validator for the
registry's schema subset (object type, typed properties, required,
additionalProperties) is implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.control.binding import (
    MalformedReference,
    dependency_closure,
    is_reference,
    parse_expects,
    parse_path,
    parse_reference,
    static_type_compatible,
)
from app.control.budget import DEFAULT_CAPS, BudgetCaps
from app.control.models import ExecutionPlan, GuardResult, PlanStep, ReasonCode
from app.control.registry import ToolRegistry

_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _matches_type(value: Any, expected: str) -> bool:
    py = _TYPE_MAP[expected]
    # bool is a subclass of int; keep integer/number checks strict.
    if expected in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, py)


def _validate_value(value: Any, schema: dict[str, Any]) -> bool:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, t) for t in expected):
            return False
    elif isinstance(expected, str):
        if not _matches_type(value, expected):
            return False
    if isinstance(value, list) and "items" in schema:
        return all(_validate_value(item, schema["items"]) for item in value)
    if isinstance(value, dict) and "properties" in schema:
        return all(
            _validate_value(v, schema["properties"][k])
            for k, v in value.items()
            if k in schema["properties"]
        )
    return True


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return a list of reason codes; empty means valid."""
    codes: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    allow_extra = schema.get("additionalProperties", True)

    for key in required:
        if key not in arguments:
            codes.append(f"{ReasonCode.GUARD_MISSING_ARGUMENT.value}:{key}")
    if not allow_extra:
        for key in arguments:
            if key not in properties:
                codes.append(f"{ReasonCode.GUARD_UNEXPECTED_ARGUMENT.value}:{key}")
    for key, value in arguments.items():
        if key in properties and not _validate_value(value, properties[key]):
            codes.append(f"{ReasonCode.GUARD_ARGUMENT_TYPE_MISMATCH.value}:{key}")
    return codes


def _has_cycle(steps: list[PlanStep]) -> bool:
    deps = {s.step_id: list(s.depends_on) for s in steps}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for parent in deps.get(node, []):
            if parent in deps and visit(parent):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    return any(visit(s.step_id) for s in steps)


@dataclass(frozen=True)
class SchemaGuard:
    registry: ToolRegistry
    caps: BudgetCaps = DEFAULT_CAPS

    def validate(self, plan: ExecutionPlan) -> GuardResult:
        codes: list[str] = []

        seen: set[str] = set()
        for step in plan.steps:
            if step.step_id in seen:
                codes.append(f"{ReasonCode.GUARD_DUPLICATE_STEP_ID.value}:{step.step_id}")
            seen.add(step.step_id)

        for step in plan.steps:
            spec = self.registry.get(step.tool)
            if spec is None:
                codes.append(f"{ReasonCode.GUARD_UNKNOWN_TOOL.value}:{step.tool}")
                continue
            codes.extend(self._validate_step_arguments(step, spec.arguments_schema, plan, seen))
            if step.risk != spec.risk:
                codes.append(
                    f"{ReasonCode.GUARD_RISK_MISMATCH.value}:{step.step_id}"
                    f"({step.risk.value}!={spec.risk.value})"
                )
            for dep in step.depends_on:
                if dep not in seen:
                    codes.append(f"{ReasonCode.GUARD_UNKNOWN_DEPENDENCY.value}:{step.step_id}->{dep}")

        if _has_cycle(plan.steps):
            codes.append(ReasonCode.GUARD_CYCLIC_DEPENDENCY.value)

        budgets = plan.budgets
        for field_name, cap in (
            ("max_tokens", self.caps.max_tokens),
            ("max_tool_calls", self.caps.max_tool_calls),
            ("max_wall_time_ms", self.caps.max_wall_time_ms),
        ):
            value = getattr(budgets, field_name)
            if value <= 0:
                codes.append(f"{ReasonCode.GUARD_BUDGET_NOT_POSITIVE.value}:{field_name}")
            elif value > cap:
                codes.append(f"{ReasonCode.GUARD_BUDGET_EXCEEDS_CAP.value}:{field_name}>{cap}")

        if len(plan.steps) > budgets.max_tool_calls:
            codes.append(
                f"{ReasonCode.GUARD_TOO_MANY_STEPS.value}:{len(plan.steps)}>{budgets.max_tool_calls}"
            )

        return GuardResult(ok=not codes, reason_codes=codes)

    def _validate_step_arguments(
        self, step: PlanStep, schema: dict, plan: ExecutionPlan, seen: set[str]
    ) -> list[str]:
        """Literal schema validation plus static step-output-reference checks.

        Referenced keys are exempt from literal required/type checks (their
        value comes from an earlier step's output); each reference itself must
        name an existing step inside the dependency closure, use valid path
        syntax, and declare a type statically compatible with the target
        argument. Pure-literal plans take exactly the old code path.
        """
        codes: list[str] = []
        properties = schema.get("properties", {})
        allow_extra = schema.get("additionalProperties", True)

        bound: dict[str, Any] = {}
        for key, value in step.arguments.items():
            if not is_reference(value):
                continue
            try:
                reference = parse_reference(value)
            except MalformedReference:
                codes.append(f"{ReasonCode.GUARD_BINDING_INVALID_PATH.value}:{step.step_id}:{key}")
                continue
            codes.extend(self._validate_binding(step, key, reference, schema, plan, seen))
            if key not in properties and not allow_extra:
                codes.append(f"{ReasonCode.GUARD_UNEXPECTED_ARGUMENT.value}:{key}")
            else:
                bound[key] = reference

        if not bound:
            return codes + validate_arguments(step.arguments, schema)

        schema_view = dict(schema)
        schema_view["required"] = [r for r in schema.get("required", []) if r not in bound]
        literal_args = {k: v for k, v in step.arguments.items() if k not in bound}
        return codes + validate_arguments(literal_args, schema_view)

    def _validate_binding(
        self, step: PlanStep, key: str, reference, schema: dict, plan: ExecutionPlan, seen: set[str]
    ) -> list[str]:
        codes: list[str] = []
        if reference.from_step not in seen:
            codes.append(
                f"{ReasonCode.GUARD_BINDING_UNKNOWN_STEP.value}:{step.step_id}->{reference.from_step}"
            )
            return codes
        steps_by_id = {s.step_id: s for s in plan.steps}
        if reference.from_step not in dependency_closure(step, steps_by_id):
            codes.append(
                f"{ReasonCode.GUARD_BINDING_NOT_IN_CLOSURE.value}:{step.step_id}->{reference.from_step}"
            )
        if parse_path(reference.path) is None:
            codes.append(f"{ReasonCode.GUARD_BINDING_INVALID_PATH.value}:{step.step_id}:{key}")
        if parse_expects(reference.expects) is None or not static_type_compatible(
            reference.expects, schema.get("properties", {}).get(key, {})
        ):
            codes.append(f"{ReasonCode.GUARD_BINDING_TYPE_MISMATCH.value}:{step.step_id}:{key}")
        return codes
