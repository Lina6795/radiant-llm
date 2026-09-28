"""RADIANT-Control M2: rule-based Planner skeleton.

The planner only *proposes* an ExecutionPlan (as a raw JSON-like dict). It
receives a metadata-only tool catalog and never sees tool handlers, so it has
no execution capability. Its output must pass the Schema Guard and the Policy
Engine before anything runs; exceptions or malformed output fail closed in
the scheduler.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.control.models import Intent, RouterDecision

_DEFAULT_BUDGETS = {"max_tokens": 8000, "max_tool_calls": 8, "max_wall_time_ms": 120000}


@dataclass(frozen=True)
class RulePlanner:
    """Produces a raw plan dict from a RouterDecision using the tool catalog."""

    tool_catalog: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, decision: RouterDecision, goal: str) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        available = {entry["name"] for entry in self.tool_catalog}

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
        # S1-7C: read-only knowledge/visual QA continues with evidence.inspect whose
        # evidence_ids is a structured step-output reference (ADR-0003) --
        # resolved at execution time from s1-search's real output, never
        # hardcoded. report/export is out of the first vertical slice.
        if (
            decision.intent in (Intent.KNOWLEDGE_QA, Intent.VISUAL_QA)
            and "evidence.inspect" in available
            and steps
        ):
            steps.append(
                {
                    "step_id": "s2-inspect",
                    "tool": "evidence.inspect",
                    "arguments": {
                        "evidence_ids": {
                            "ref": "step_output",
                            "from_step": "s1-search",
                            "path": "output.hits[*].evidence_id",
                            "expects": "array<string>",
                        }
                    },
                    "depends_on": ["s1-search"],
                    "risk": "read_only",
                    "timeout_ms": 5000,
                    "retry_policy": "none",
                    "idempotency_key": None,
                }
            )
        # S4-5: assemble the budgeted ContextPackage from the inspected
        # evidence (bindings resolve from the two real step outputs). This is
        # the trace any future answering model call must consume first.
        if (
            decision.intent in (Intent.KNOWLEDGE_QA, Intent.VISUAL_QA)
            and "context.assemble" in available
            and len(steps) == 2
        ):
            steps.append(
                {
                    "step_id": "s3-context",
                    "tool": "context.assemble",
                    "arguments": {
                        "evidence_records": {
                            "ref": "step_output",
                            "from_step": "s2-inspect",
                            "path": "output.evidence",
                            "expects": "array<object>",
                        },
                        "question": {
                            "ref": "step_output",
                            "from_step": "s1-search",
                            "path": "output.query",
                            "expects": "string",
                        },
                    },
                    "depends_on": ["s2-inspect"],
                    "risk": "read_only",
                    "timeout_ms": 10000,
                    "retry_policy": "none",
                    "idempotency_key": None,
                }
            )
        # S6: bounded answer chain -- draft from the ContextPackage, then the
        # Verifier (accept / one revise / review / abstain / clarify).
        if (
            decision.intent in (Intent.KNOWLEDGE_QA, Intent.VISUAL_QA)
            and "answer.draft" in available
            and "answer.verify" in available
            and len(steps) == 3
        ):
            steps.append(
                {
                    "step_id": "s4-draft",
                    "tool": "answer.draft",
                    "arguments": {
                        "context_package": {
                            "ref": "step_output",
                            "from_step": "s3-context",
                            "path": "output.context_package",
                            "expects": "object",
                        },
                        "question": {
                            "ref": "step_output",
                            "from_step": "s1-search",
                            "path": "output.query",
                            "expects": "string",
                        },
                    },
                    "depends_on": ["s3-context"],
                    "risk": "read_only",
                    "timeout_ms": 120000,
                    "retry_policy": "transient_only",
                    "idempotency_key": None,
                }
            )
            steps.append(
                {
                    "step_id": "s5-verify",
                    "tool": "answer.verify",
                    "arguments": {
                        "draft": {
                            "ref": "step_output",
                            "from_step": "s4-draft",
                            "path": "output.draft",
                            "expects": "string",
                        },
                        "evidence_records": {
                            "ref": "step_output",
                            "from_step": "s2-inspect",
                            "path": "output.evidence",
                            "expects": "array<object>",
                        },
                        "question": {
                            "ref": "step_output",
                            "from_step": "s1-search",
                            "path": "output.query",
                            "expects": "string",
                        },
                        "allow_revise": True,
                    },
                    "depends_on": ["s4-draft", "s2-inspect", "s1-search"],
                    "risk": "read_only",
                    "timeout_ms": 120000,
                    "retry_policy": "none",
                    "idempotency_key": None,
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
