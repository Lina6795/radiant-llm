"""Step output binding primitives (ADR-0003).

Shared by the Schema Guard (static validation, S1-7A) and the DurableRunner
(runtime resolution, S1-7B). References are controlled dict values embedded in
``PlanStep.arguments`` with the discriminating marker ``ref="step_output"``;
anything else is an ordinary literal. Path syntax is the restricted subset
``output(.key|[*])+`` -- a trailing ``[*]`` is rejected because the flattened
element type is ambiguous for static checking.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from app.control.models import PlanStep, StepOutputReference

REFERENCE_MARKER = "step_output"

_SCALAR_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
_EXPECTS_RE = re.compile(r"^(array<([a-z]+)>|([a-z]+))$")
_PATH_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[\*\]")


_REQUIRED_KEYS = frozenset({"ref", "from_step", "path", "expects"})


def is_reference(value: Any) -> bool:
    """True only for dicts carrying the full discriminating marker shape.

    A dict with only a partial marker (e.g. just ``{"ref": "step_output"}``)
    is an ordinary literal; misclassifying it would corrupt both literal
    schema checks and binding semantics.
    """
    return (
        isinstance(value, dict)
        and value.get("ref") == REFERENCE_MARKER
        and _REQUIRED_KEYS.issubset(value)
    )


class MalformedReference(ValueError):
    """A dict carries the marker but is not a valid StepOutputReference."""


def parse_reference(value: Any) -> StepOutputReference:
    """Parse a marked dict into StepOutputReference; raise MalformedReference."""
    if not isinstance(value, dict) or value.get("ref") != REFERENCE_MARKER:
        raise MalformedReference("not a step_output reference")
    allowed = {"ref", "from_step", "path", "expects"}
    if any(k not in allowed for k in value):
        raise MalformedReference(f"unexpected keys: {sorted(set(value) - allowed)}")
    try:
        return StepOutputReference(**value)
    except Exception as exc:  # pydantic ValidationError -> typed guard error
        raise MalformedReference(str(exc)) from exc


def parse_path(path: str) -> Optional[list[tuple[str, str]]]:
    """Parse the restricted path grammar into [('key', name) | ('star', '*')].

    Returns None for any syntax outside the subset (bad start, numeric index,
    slice, trailing star, empty remainder garbage).
    """
    if not path or not path.startswith("output"):
        return None
    rest = path[len("output"):]
    segments: list[tuple[str, str]] = []
    pos = 0
    while pos < len(rest):
        match = _PATH_TOKEN_RE.match(rest, pos)
        if match is None:
            return None
        if match.group(1) is not None:
            segments.append(("key", match.group(1)))
        else:
            segments.append(("star", "*"))
        pos = match.end()
    if segments and segments[-1][0] == "star":
        return None
    return segments


def parse_expects(expects: str) -> Optional[tuple[str, str]]:
    """Return ('array', item_type) or ('scalar', type); None if malformed."""
    match = _EXPECTS_RE.match(expects or "")
    if not match:
        return None
    if match.group(2) is not None:
        item = match.group(2)
        if item not in _SCALAR_TYPES - {"array"}:
            return None
        return ("array", item)
    scalar = match.group(3)
    if scalar not in _SCALAR_TYPES:
        return None
    return ("scalar", scalar)


def static_type_compatible(expects: str, schema: dict) -> bool:
    """Static compatibility between declared expects and the target argument
    schema. Absent type information is compatible (runtime re-validation is
    authoritative); a present incompatible type is not."""
    parsed = parse_expects(expects)
    if parsed is None:
        return False
    container, item = parsed
    target_type = schema.get("type")
    targets = target_type if isinstance(target_type, list) else [target_type]
    if container == "array":
        if not any(t in ("array", None) for t in targets):
            return False
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            it = items_schema.get("type")
            its = it if isinstance(it, list) else [it]
            if not any(t in (item, None) for t in its):
                return False
        return True
    return any(t in (item, None) for t in targets)


def dependency_closure(step: PlanStep, steps_by_id: dict[str, PlanStep]) -> set[str]:
    """All step ids transitively reachable via depends_on (existing deps only)."""
    closure: set[str] = set()
    stack = [d for d in step.depends_on if d in steps_by_id]
    while stack:
        dep = stack.pop()
        if dep in closure:
            continue
        closure.add(dep)
        stack.extend(d for d in steps_by_id[dep].depends_on if d in steps_by_id)
    return closure


# ----------------------------------------------------------------------
# Runtime resolution (S1-7B): used by the DurableRunner before invoke.
# ----------------------------------------------------------------------

_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class BindingResolutionError(Exception):
    """Typed binding failure; ``code`` is a stable runtime.* reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_path(node: Any, segments: list[tuple[str, str]]) -> list[Any]:
    """Evaluate parsed path segments; [*] expands and flattens."""
    if not segments:
        return [node]
    kind, name = segments[0]
    rest = segments[1:]
    if kind == "key":
        if not isinstance(node, dict) or name not in node:
            raise BindingResolutionError(
                "runtime.binding_path_missing", f"path segment '{name}' not found"
            )
        return _walk_path(node[name], rest)
    if not isinstance(node, list):
        raise BindingResolutionError(
            "runtime.binding_type_mismatch", "[*] applied to a non-list value"
        )
    out: list[Any] = []
    for element in node:
        out.extend(_walk_path(element, rest))
    return out


def _value_matches_declared(value: Any, expects: str) -> bool:
    parsed = parse_expects(expects)
    if parsed is None:
        return False
    container, item = parsed
    py = _PY_TYPES[item]
    strict_num = item in ("integer", "number")

    def _ok(v: Any) -> bool:
        if strict_num and isinstance(v, bool):
            return False
        return isinstance(v, py)

    if container == "array":
        return isinstance(value, list) and all(_ok(v) for v in value)
    return _ok(value)


def resolve_step_arguments(
    step: PlanStep, arguments_schema: dict, source_outputs: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve step-output references into literal arguments.

    ``source_outputs`` maps step_id -> succeeded ToolResult output (read from
    checkpoints, so resume/fencing re-resolution is identical). Returns the
    resolved argument dict plus one trace record per resolved reference
    (step_id, arg_key, from_step, path, expects, count, digest -- never the
    resolved values themselves). Raises BindingResolutionError (typed) on any
    failure; the caller must not invoke the tool in that case.
    """
    resolved: dict[str, Any] = {}
    trace_records: list[dict[str, Any]] = []

    for key, value in step.arguments.items():
        if not is_reference(value):
            resolved[key] = value
            continue
        try:
            reference = parse_reference(value)
        except MalformedReference as exc:
            raise BindingResolutionError(
                "runtime.binding_path_missing", f"malformed reference for '{key}': {exc}"
            ) from exc
        segments = parse_path(reference.path)
        if segments is None:
            raise BindingResolutionError(
                "runtime.binding_path_missing", f"unparseable path '{reference.path}'"
            )
        if reference.from_step not in source_outputs:
            raise BindingResolutionError(
                "runtime.binding_source_not_succeeded",
                f"source step '{reference.from_step}' has no succeeded output",
            )
        values = _walk_path(source_outputs[reference.from_step], segments)
        has_star = any(kind == "star" for kind, _ in segments)
        if "array<" in reference.expects:
            if has_star:
                unfolded: Any = values
            else:
                # no expansion: the path must yield exactly one node that IS
                # the list (e.g. output.evidence)
                if len(values) != 1 or not isinstance(values[0], list):
                    raise BindingResolutionError(
                        "runtime.binding_type_mismatch",
                        f"path '{reference.path}' must resolve to a single list for '{key}'",
                    )
                unfolded = values[0]
        else:
            if len(values) != 1:
                raise BindingResolutionError(
                    "runtime.binding_type_mismatch",
                    f"path '{reference.path}' produced {len(values)} values for scalar argument '{key}'",
                )
            unfolded = values[0] if values else None
        if not _value_matches_declared(unfolded, reference.expects):
            raise BindingResolutionError(
                "runtime.binding_type_mismatch",
                f"resolved value for '{key}' does not match expects={reference.expects}",
            )
        required = arguments_schema.get("required", [])
        if unfolded == [] and key in required:
            raise BindingResolutionError(
                "runtime.binding_empty",
                f"reference for required argument '{key}' expanded to an empty list",
            )
        resolved[key] = unfolded
        digest = hashlib.sha256(
            json.dumps(unfolded, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        trace_records.append(
            {
                "step_id": step.step_id,
                "arg_key": key,
                "from_step": reference.from_step,
                "path": reference.path,
                "expects": reference.expects,
                "count": len(values) if isinstance(unfolded, list) else 1,
                "digest": digest,
            }
        )
    return resolved, trace_records
