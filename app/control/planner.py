"""RADIANT-Control M2: rule-based Planner skeleton.

The planner only *proposes* an ExecutionPlan (as a raw JSON-like dict). It
receives a metadata-only tool catalog and never sees tool handlers, so it has
no execution capability. Its output must pass the Schema Guard and the Policy
Engine before anything runs; exceptions or malformed output fail closed in
the scheduler.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.control.models import RouterDecision

_DEFAULT_BUDGETS = {"max_tokens": 8000, "max_tool_calls": 4, "max_wall_time_ms": 30000}


@dataclass(frozen=True)
class RulePlanner:
    """Produces a raw plan dict from a RouterDecision using the tool catalog."""

    tool_catalog: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, decision: RouterDecision, goal: str) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        available = {entry["name"] for entry in self.tool_catalog}

        lowered = goal.lower()
        wants_report = bool(re.search(r"\bexport\b|\breport\b", lowered))

        steps: list[dict[str, Any]] = []
        if "evidence.search" in available:
            steps.append(
                {
                    "step_id": "s1-search",
                    "tool": "evidence.search",
                    "arguments": {"query": goal, "top_k": 3},
                    "depends_on": [],
                    "risk": "read_only",
                    "timeout_ms": 5000,
                    "retry_policy": "transient_only",
                    "idempotency_key": None,
                }
            )
        if wants_report and "report.export" in available:
            steps.append(
                {
                    "step_id": "s2-export",
                    "tool": "report.export",
                    "arguments": {
                        "title": f"Report: {goal[:60]}",
                        "content": "mock report body",
                        "idempotency_key": f"{run_id}:export",
                    },
                    "depends_on": ["s1-search"] if steps else [],
                    "risk": "bounded_write",
                    "timeout_ms": 5000,
                    "retry_policy": "none",
                    "idempotency_key": f"{run_id}:export",
                }
            )

        return {
            "run_id": run_id,
            "goal": goal,
            "steps": steps,
            "budgets": dict(_DEFAULT_BUDGETS),
        }


# Type alias for dependency injection. A planner callable returns a raw
# JSON-like dict (or an ExecutionPlan); the scheduler always validates it into
# the ExecutionPlan contract before the Schema Guard sees it.
PlannerCallable = Callable[[RouterDecision, str], Any]
