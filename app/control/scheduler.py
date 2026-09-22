"""RADIANT-Control M2: minimal sequential scheduler.

Pipeline: router -> planner -> Schema Guard -> Policy Engine -> execute ->
summarize. The planner's output can only reach the executor through the guard
and the policy engine; planner exceptions or malformed output fail closed
(rejected, zero tools executed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.control.budget import BudgetLedger
from app.control.models import (
    Action,
    ExecutionPlan,
    PlanStep,
    PolicyVerdict,
    ReasonCode,
    RouterDecision,
    RunStatus,
    RunSummary,
    ToolError,
    ToolMetrics,
    ToolResult,
    ToolStatus,
)
from app.control.planner import PlannerCallable, RulePlanner
from app.control.policy import PolicyEngine
from app.control.registry import ToolRegistry, build_default_registry
from app.control.router import RouterCallable, RuleRouter
from app.control.schema_guard import SchemaGuard


def _topological_order(steps: list[PlanStep]) -> list[PlanStep]:
    """Stable topological order; guard already proved the DAG is acyclic."""
    by_id = {s.step_id: s for s in steps}
    done: set[str] = set()
    ordered: list[PlanStep] = []
    remaining = list(steps)
    while remaining:
        progressed = False
        for step in list(remaining):
            if all(dep in done for dep in step.depends_on):
                ordered.append(step)
                done.add(step.step_id)
                remaining.remove(step)
                progressed = True
        if not progressed:
            # Unreachable given the guard; stay fail closed.
            break
    return ordered


@dataclass
class ControlPlane:
    registry: ToolRegistry
    policy: PolicyEngine
    guard: SchemaGuard
    router: RouterCallable
    planner: PlannerCallable

    @classmethod
    def build(
        cls,
        router: RouterCallable | None = None,
        planner: PlannerCallable | None = None,
        registry: ToolRegistry | None = None,
        ledger: BudgetLedger | None = None,
        workspace_acls: dict[str, set[str]] | None = None,
    ) -> "ControlPlane":
        reg = registry or build_default_registry()
        led = ledger or BudgetLedger.with_defaults(["default", "readonly", "lowbudget", "full"])
        policy_kwargs = {"workspace_acls": workspace_acls} if workspace_acls else {}
        policy = PolicyEngine(registry=reg, ledger=led, **policy_kwargs)
        return cls(
            registry=reg,
            policy=policy,
            guard=SchemaGuard(registry=reg),
            router=router or RuleRouter(),
            planner=planner or RulePlanner(tool_catalog=reg.catalog()),
        )

    def run(self, goal: str, workspace: str = "default") -> RunSummary:
        # 1. Route. Router errors fail closed into clarify.
        try:
            decision = self.router(goal)
        except Exception as exc:
            return RunSummary(
                status=RunStatus.CLARIFIED,
                reason_codes=[f"{ReasonCode.ROUTER_ERROR.value}:{type(exc).__name__}"],
            )

        if decision.action == Action.RESPOND:
            return RunSummary(status=RunStatus.RESPONDED, reason_codes=list(decision.reason_codes), decision=decision)
        if decision.action == Action.CLARIFY:
            return RunSummary(status=RunStatus.CLARIFIED, reason_codes=list(decision.reason_codes), decision=decision)
        if decision.action == Action.ABSTAIN:
            return RunSummary(status=RunStatus.ABSTAINED, reason_codes=list(decision.reason_codes), decision=decision)

        # 2. Plan. The planner never touches the executor; its raw output is
        # validated into the contract before the guard sees it.
        try:
            raw_plan = self.planner(decision, goal)
        except Exception as exc:
            return RunSummary(
                status=RunStatus.REJECTED,
                reason_codes=[f"{ReasonCode.PLANNER_ERROR.value}:{type(exc).__name__}"],
                decision=decision,
            )
        try:
            plan = raw_plan if isinstance(raw_plan, ExecutionPlan) else ExecutionPlan.model_validate(raw_plan)
        except Exception:
            return RunSummary(
                status=RunStatus.REJECTED,
                reason_codes=[ReasonCode.PLANNER_INVALID_OUTPUT.value],
                decision=decision,
            )

        # 3. Schema Guard.
        guard_result = self.guard.validate(plan)
        if not guard_result.ok:
            return RunSummary(
                status=RunStatus.REJECTED,
                reason_codes=list(guard_result.reason_codes),
                decision=decision,
                plan=plan,
            )

        # 4. Policy Engine.
        policy = self.policy.authorize(plan, workspace)
        if policy.verdict == PolicyVerdict.DENY:
            return RunSummary(
                status=RunStatus.REJECTED,
                reason_codes=list(policy.reason_codes),
                decision=decision,
                plan=plan,
                policy=policy,
            )
        if policy.verdict == PolicyVerdict.REVIEW:
            return RunSummary(
                status=RunStatus.NEEDS_REVIEW,
                reason_codes=list(policy.reason_codes),
                decision=decision,
                plan=plan,
                policy=policy,
            )

        # 5. Execute sequentially in dependency order, enforcing max_tool_calls.
        results: list[ToolResult] = []
        executed = 0
        tokens_used = 0
        started = time.monotonic()
        for step in _topological_order(plan.steps):
            if executed >= plan.budgets.max_tool_calls:
                results.append(
                    ToolResult(
                        status=ToolStatus.DENIED,
                        error=ToolError(
                            code=ReasonCode.RUNTIME_BUDGET_EXHAUSTED.value,
                            message=f"max_tool_calls {plan.budgets.max_tool_calls} exhausted before {step.step_id}",
                            retryable=False,
                        ),
                        metrics=ToolMetrics(latency_ms=0, token_count=0),
                        provenance=self.registry.provenance_for(step.tool),
                    )
                )
                continue
            if (time.monotonic() - started) * 1000 > plan.budgets.max_wall_time_ms:
                results.append(
                    ToolResult(
                        status=ToolStatus.DENIED,
                        error=ToolError(
                            code=ReasonCode.RUNTIME_BUDGET_EXHAUSTED.value,
                            message=f"max_wall_time_ms exceeded before {step.step_id}",
                            retryable=False,
                        ),
                        metrics=ToolMetrics(latency_ms=0, token_count=0),
                        provenance=self.registry.provenance_for(step.tool),
                    )
                )
                continue
            result = self.registry.invoke(step.tool, step.arguments, run_id=str(plan.run_id), workspace=workspace)
            executed += 1
            tokens_used += result.metrics.token_count
            results.append(result)

        self.policy.ledger.record(workspace, tokens_used)

        failed = any(r.status == ToolStatus.TERMINAL_ERROR for r in results)
        return RunSummary(
            status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
            reason_codes=[ReasonCode.POLICY_ALLOWED.value],
            decision=decision,
            plan=plan,
            policy=policy,
            results=results,
            tools_executed=executed,
        )
