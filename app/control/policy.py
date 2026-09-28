"""RADIANT-Control M2: Policy Engine.

Decides allow / deny / review for a guard-validated ExecutionPlan based on:
- workspace ACL (workspace -> allowed tool names);
- tool risk level (external and high-risk tools require review; nothing
  external executes without human sign-off in M2);
- idempotency requirements for side-effecting tools;
- remaining workspace budget.

The response_contract of the RouterDecision is never consulted here: answer
formatting requirements cannot influence tool authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.control.budget import BudgetLedger
from app.control.models import (
    ExecutionPlan,
    PolicyDecision,
    PolicyVerdict,
    ReasonCode,
    Risk,
)
from app.control.registry import ToolRegistry

# Workspace ACLs for the M2 skeleton. A workspace not listed here is denied
# every tool (fail closed).
DEFAULT_WORKSPACE_ACLS: dict[str, set[str]] = {
    "default": {"evidence.search", "evidence.inspect", "citation.validate", "report.export", "context.assemble", "answer.draft", "answer.verify"},
    "readonly": {"evidence.search", "evidence.inspect", "citation.validate", "context.assemble", "answer.draft", "answer.verify"},
    "lowbudget": {"evidence.search", "evidence.inspect", "citation.validate", "report.export", "context.assemble", "answer.draft", "answer.verify"},
    # Privileged workspace: every registered tool is in the ACL, but external
    # and high-risk tools still require review and legacy metadata-only tools
    # still cannot execute in M2.
    "full": {
        "evidence.search", "evidence.inspect", "citation.validate", "report.export",
        "context.assemble", "answer.draft", "answer.verify",
        "PDFReaderTool", "PDFKnowledgeBaseSanitizerTool", "URLValidationTool",
        "WebSearchTool", "WebScraperTool", "WikipediaSearchTool", "PythonREPLTool",
        "ImageAnalysisTool", "CSVandExcelFileParserTool", "CSVDataFinderTool",
        "TextFileReaderTool", "SkillLookupTool", "FileDownloaderTool",
    },
}


@dataclass(frozen=True)
class PolicyEngine:
    registry: ToolRegistry
    ledger: BudgetLedger
    workspace_acls: dict[str, set[str]] = field(default_factory=lambda: {k: set(v) for k, v in DEFAULT_WORKSPACE_ACLS.items()})

    def authorize(self, plan: ExecutionPlan, workspace: str) -> PolicyDecision:
        allowed_tools = self.workspace_acls.get(workspace, set())

        for step in plan.steps:
            spec = self.registry.get(step.tool)
            if spec is None:
                # Guard should have caught this; stay fail closed.
                return PolicyDecision(
                    verdict=PolicyVerdict.DENY,
                    reason_codes=[ReasonCode.GUARD_UNKNOWN_TOOL.value],
                    detail=f"unregistered tool {step.tool}",
                )
            if step.tool not in allowed_tools:
                return PolicyDecision(
                    verdict=PolicyVerdict.DENY,
                    reason_codes=[ReasonCode.POLICY_WORKSPACE_DENIED.value],
                    detail=f"tool {step.tool} not in workspace ACL '{workspace}'",
                )

        for step in plan.steps:
            spec = self.registry.get(step.tool)
            if spec.risk == Risk.BOUNDED_WRITE and not step.idempotency_key:
                return PolicyDecision(
                    verdict=PolicyVerdict.DENY,
                    reason_codes=[ReasonCode.POLICY_MISSING_IDEMPOTENCY_KEY.value],
                    detail=f"side-effecting tool {step.tool} requires idempotency_key",
                )

        review_codes: list[str] = []
        for step in plan.steps:
            spec = self.registry.get(step.tool)
            if spec.risk == Risk.EXTERNAL:
                review_codes.append(f"{ReasonCode.POLICY_EXTERNAL_REVIEW.value}:{step.tool}")
            elif spec.high_risk:
                review_codes.append(f"{ReasonCode.POLICY_HIGH_RISK_REVIEW.value}:{step.tool}")
        if review_codes:
            return PolicyDecision(
                verdict=PolicyVerdict.REVIEW,
                reason_codes=review_codes,
                detail="external/high-risk tools require human review before execution",
            )

        remaining = self.ledger.remaining(workspace)
        if plan.budgets.max_tokens > remaining:
            return PolicyDecision(
                verdict=PolicyVerdict.DENY,
                reason_codes=[ReasonCode.POLICY_BUDGET_EXCEEDED.value],
                detail=f"plan budget {plan.budgets.max_tokens} tokens exceeds remaining {remaining}",
            )

        return PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            reason_codes=[ReasonCode.POLICY_ALLOWED.value],
        )
