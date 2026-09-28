"""RADIANT-Control M2: control-plane data contracts (pydantic v2).

These models are the frozen contract between Router, Planner, Schema Guard,
Policy Engine, Budget Controller and the Scheduler. All models forbid extra
fields so that a planner cannot smuggle unreviewed keys through the pipeline.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ReasonCode(str, Enum):
    """Stable reason codes. Every deny / clarify / abstain must carry at least one."""

    # Router
    ROUTER_KEYWORD_MATCH = "router.keyword_match"
    ROUTER_DEFAULT_INTENT = "router.default_intent"
    ROUTER_LOW_CONFIDENCE = "router.low_confidence"
    ROUTER_EMPTY_INPUT = "router.empty_input"
    ROUTER_INJECTION_SUSPECTED = "router.injection_suspected"
    ROUTER_CITATION_DEMAND = "router.citation_demand"
    ROUTER_ERROR = "router.error"

    # Planner
    PLANNER_ERROR = "planner.error"
    PLANNER_INVALID_OUTPUT = "planner.invalid_output"

    # Schema Guard
    GUARD_UNKNOWN_TOOL = "guard.unknown_tool"
    GUARD_MISSING_ARGUMENT = "guard.missing_argument"
    GUARD_ARGUMENT_TYPE_MISMATCH = "guard.argument_type_mismatch"
    GUARD_UNEXPECTED_ARGUMENT = "guard.unexpected_argument"
    GUARD_CYCLIC_DEPENDENCY = "guard.cyclic_dependency"
    GUARD_UNKNOWN_DEPENDENCY = "guard.unknown_dependency"
    GUARD_DUPLICATE_STEP_ID = "guard.duplicate_step_id"
    GUARD_BUDGET_NOT_POSITIVE = "guard.budget_not_positive"
    GUARD_BUDGET_EXCEEDS_CAP = "guard.budget_exceeds_cap"
    GUARD_RISK_MISMATCH = "guard.risk_mismatch"
    GUARD_TOO_MANY_STEPS = "guard.too_many_steps"
    GUARD_BINDING_UNKNOWN_STEP = "guard.binding_unknown_step"
    GUARD_BINDING_NOT_IN_CLOSURE = "guard.binding_not_in_closure"
    GUARD_BINDING_INVALID_PATH = "guard.binding_invalid_path"
    GUARD_BINDING_TYPE_MISMATCH = "guard.binding_type_mismatch"

    # Policy Engine
    POLICY_ALLOWED = "policy.allowed"
    POLICY_WORKSPACE_DENIED = "policy.workspace_denied"
    POLICY_MISSING_IDEMPOTENCY_KEY = "policy.missing_idempotency_key"
    POLICY_EXTERNAL_REVIEW = "policy.external_review"
    POLICY_HIGH_RISK_REVIEW = "policy.high_risk_review"
    POLICY_BUDGET_EXCEEDED = "policy.budget_exceeded"

    # Runtime / tools
    TOOL_NOT_IMPLEMENTED = "tool.not_implemented"
    TOOL_EXECUTION_ERROR = "tool.execution_error"
    RUNTIME_BUDGET_EXHAUSTED = "runtime.budget_exhausted"
    RUNTIME_BINDING_PATH_MISSING = "runtime.binding_path_missing"
    RUNTIME_BINDING_TYPE_MISMATCH = "runtime.binding_type_mismatch"
    RUNTIME_BINDING_EMPTY = "runtime.binding_empty"
    RUNTIME_BINDING_SOURCE_NOT_SUCCEEDED = "runtime.binding_source_not_succeeded"


class Intent(str, Enum):
    KNOWLEDGE_QA = "knowledge_qa"
    VISUAL_QA = "visual_qa"
    COMPARE = "compare"
    INGEST = "ingest"
    AUDIT = "audit"


class Action(str, Enum):
    RESPOND = "respond"
    TOOL_CALL = "tool_call"
    CLARIFY = "clarify"
    ABSTAIN = "abstain"


class ResponseFormat(str, Enum):
    ANSWER = "answer"
    TABLE = "table"
    REPORT = "report"
    TRACE = "trace"


class Risk(str, Enum):
    READ_ONLY = "read_only"
    BOUNDED_WRITE = "bounded_write"
    EXTERNAL = "external"


class RetryPolicy(str, Enum):
    TRANSIENT_ONLY = "transient_only"
    NONE = "none"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"
    DENIED = "denied"


class PolicyVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"


class RunStatus(str, Enum):
    RESPONDED = "responded"
    CLARIFIED = "clarified"
    ABSTAINED = "abstained"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    FAILED = "failed"


class QueryRequest(BaseModel):
    """Entry contract for the first vertical slice (read-only PDF QA).
    Replaces ad-hoc dicts at the API boundary so malformed requests fail
    at validation time instead of deep inside the chain."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    workspace_id: str = "default"
    session_id: Optional[str] = None


class ResponseContract(BaseModel):
    """Answer-format requirement. Deliberately decoupled from tool routing:
    the Policy Engine never reads this field, so formatting demands can never
    widen or narrow tool authorization."""

    model_config = ConfigDict(extra="forbid")

    format: ResponseFormat = ResponseFormat.ANSWER
    citation_required: bool = False


class RouterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    action: Action
    confidence: float = Field(ge=0.0, le=1.0)
    working_subject: Optional[str] = None
    reason_codes: list[str] = Field(min_length=1)
    response_contract: ResponseContract = Field(default_factory=ResponseContract)


class StepOutputReference(BaseModel):
    """Controlled value type embedded in PlanStep.arguments (ADR-0003).

    Marks an argument whose value must be resolved at execution time from the
    succeeded checkpoint output of an earlier step. ``ref`` is the
    discriminating marker: dicts without it are ordinary literals.
    ``path`` uses the restricted syntax ``output(.key|[*])+`` -- no numeric
    indices, slices, or arbitrary expressions.
    """

    model_config = ConfigDict(extra="forbid")

    ref: Literal["step_output"] = "step_output"
    from_step: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expects: str = Field(min_length=1)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    risk: Risk
    timeout_ms: int = Field(gt=0)
    retry_policy: RetryPolicy = RetryPolicy.NONE
    idempotency_key: Optional[str] = None


class Budgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_wall_time_ms: int = Field(gt=0)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    goal: str
    steps: list[PlanStep]
    budgets: Budgets


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool


class ToolMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: int = Field(ge=0, default=0)
    token_count: int = Field(ge=0, default=0)


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_version: str
    config_fingerprint: str


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None
    metrics: ToolMetrics = Field(default_factory=ToolMetrics)
    provenance: Optional[Provenance] = None


class ToolSpec(BaseModel):
    """Registry entry. `handler` is only set for tools implemented in this
    process (M2: the four mocks). Metadata-only entries for the 13 legacy
    tools carry no handler and can never be executed by the scheduler."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    name: str
    version: str
    risk: Risk
    high_risk: bool = False
    description: str = ""
    # Minimal JSON-schema subset for arguments: object with typed properties,
    # required list, additionalProperties flag.
    arguments_schema: dict[str, Any] = Field(default_factory=dict)
    implemented: bool = False
    handler: Optional[Callable[..., Any]] = Field(default=None, exclude=True)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: PolicyVerdict
    reason_codes: list[str] = Field(min_length=1)
    detail: str = ""


class GuardResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason_codes: list[str] = Field(default_factory=list)
    detail: str = ""


class RunSummary(BaseModel):
    """Aggregate result of one control-plane run (guard -> policy -> execute)."""

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    reason_codes: list[str] = Field(min_length=1)
    decision: Optional[RouterDecision] = None
    plan: Optional[ExecutionPlan] = None
    policy: Optional[PolicyDecision] = None
    results: list[ToolResult] = Field(default_factory=list)
    tools_executed: int = 0
