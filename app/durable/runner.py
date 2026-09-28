"""RADIANT-Control M3: DurableRunner.

Drives an M2 ``ExecutionPlan`` through the durable state graph:

* every node state change is guarded by the legal-transition table
  (:mod:`app.durable.graph`);
* every node start/completion writes a checkpoint snapshot
  (:mod:`app.durable.checkpoint`) and an event (:mod:`app.durable.events`);
* only ``retryable_error`` results are retried, with exponential backoff on
  an injectable clock (:mod:`app.durable.retry`);
* side effects go through the persistent idempotency ledger
  (:mod:`app.durable.idempotency`), which replaces the M2 in-memory ledger
  on the registry instance handed to this runner;
* each run is protected by a lease with a fencing token
  (:mod:`app.durable.lease`) validated before every node commit.

Cancel is cooperative: ``cancel(run_id)`` sets a persisted flag plus an
in-memory :class:`CancelToken`; the runner checks both between nodes and
while a node is executing (the invoke loop polls the token, and token-aware
tools can poll it themselves via ``runner.cancel_token(run_id)``).

Resume rebuilds run state from the checkpoints of the same SQLite file --
possibly through brand-new store instances, which simulates a process kill --
and re-executes only the steps without a ``succeeded`` checkpoint. It never
restarts finished work from scratch.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.control.binding import (
    BindingResolutionError,
    is_reference,
    resolve_step_arguments,
)
from app.control.models import (
    ExecutionPlan,
    PlanStep,
    ReasonCode,
    RetryPolicy,
    ToolError,
    ToolMetrics,
    ToolResult,
    ToolStatus,
)
from app.control.registry import ToolRegistry
from app.control.schema_guard import validate_arguments
from app.durable.checkpoint import Checkpoint, CheckpointStore
from app.durable.errors import CheckpointMismatchError, DurableError, LeaseConflictError, LeaseFencingError
from app.durable.events import EventStore, EventType
from app.durable.graph import (
    RunState,
    StateGraph,
    StepState,
    TERMINAL_RUN_STATES,
    guard_run_transition,
)
from app.durable.idempotency import PersistentIdempotencyLedger
from app.durable.lease import Lease, LeaseManager
from app.durable.retry import ExponentialBackoff


class CancelToken:
    """Cooperative cancellation handle for one run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class _RunCancelledSignal(Exception):
    """Internal control flow: a running node observed cancellation."""


@dataclass
class ResumeInfo:
    """Provenance for a (possibly resumed) execution: which checkpoints were
    restored, under which configuration, and with which fencing token."""

    resumed: bool
    restored_steps: list[str] = field(default_factory=list)
    config_fingerprint: str = ""
    stored_config_fingerprint: str = ""
    owner: str = ""
    fencing_token: int = 0


@dataclass
class DurableRunReport:
    run_id: str
    status: RunState
    step_states: dict[str, StepState]
    results: dict[str, ToolResult]
    attempts: dict[str, int]
    tools_executed: int
    resume: ResumeInfo
    reason: str = ""
    fenced: bool = False


def config_fingerprint(plan: ExecutionPlan, registry: ToolRegistry) -> str:
    """Fingerprint of everything a resume must match: the plan itself plus
    the tool catalog of the registry."""
    payload = json.dumps(
        {"plan": plan.model_dump(mode="json"), "registry": registry.config_fingerprint()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class DurableRunner:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        checkpoints: CheckpointStore,
        events: EventStore,
        leases: LeaseManager,
        ledger: PersistentIdempotencyLedger | None = None,
        backoff: ExponentialBackoff | None = None,
        owner: str = "worker-0",
        lease_ttl_s: float = 30.0,
        before_node: Callable[[str, str], None] | None = None,
        require_review: Callable[[PlanStep], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.checkpoints = checkpoints
        self.events = events
        self.leases = leases
        self.backoff = backoff or ExponentialBackoff()
        self.owner = owner
        self.lease_ttl_s = lease_ttl_s
        self.before_node = before_node
        self.require_review = require_review
        self._tokens: dict[str, CancelToken] = {}
        if ledger is not None:
            # Durable path: replace the M2 in-memory ledger on this registry
            # instance (attribute injection, no changes to app/control).
            self.registry.ledger = ledger

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def cancel_token(self, run_id: str) -> CancelToken:
        return self._tokens.setdefault(run_id, CancelToken())

    def cancel(self, run_id: str) -> None:
        """Cooperative cancel: persisted flag (visible across processes) plus
        in-memory token (visible to the executing thread)."""
        self.checkpoints.request_cancel(run_id)
        self.cancel_token(run_id).cancel()

    def run(self, plan: ExecutionPlan, workspace: str = "default") -> DurableRunReport:
        run_id = str(plan.run_id)
        graph = StateGraph.from_plan(plan)
        fingerprint = config_fingerprint(plan, self.registry)
        token = self.cancel_token(run_id)
        lease = self.leases.acquire(run_id, self.owner, self.lease_ttl_s)
        self.checkpoints.create_run(
            run_id,
            goal=plan.goal,
            workspace=workspace,
            owner=self.owner,
            config_fingerprint=fingerprint,
        )
        self.checkpoints.save_plan(run_id, plan, workspace)
        self._set_run_state(run_id, RunState.PENDING, RunState.RUNNING)
        self.events.append(
            run_id,
            EventType.RUN_STARTED,
            {
                "goal": plan.goal,
                "workspace": workspace,
                "owner": self.owner,
                "config_fingerprint": fingerprint,
                "fencing_token": lease.fencing_token,
            },
        )
        self.events.append(
            run_id,
            EventType.LEASE_ACQUIRED,
            {"owner": lease.owner, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at},
        )
        resume_info = ResumeInfo(
            resumed=False,
            config_fingerprint=fingerprint,
            stored_config_fingerprint=fingerprint,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
        )
        return self._execute(plan, graph, workspace, lease, token, restored={}, resume_info=resume_info)

    def resume(
        self,
        plan: ExecutionPlan | None = None,
        workspace: str | None = None,
        *,
        run_id: str | None = None,
        owner: str | None = None,
        force_takeover: bool = False,
        allow_config_mismatch: bool = False,
    ) -> DurableRunReport:
        """Resume a run from its persisted checkpoints.

        Completed steps are restored and never re-executed; steps without a
        ``succeeded`` checkpoint are re-run from ``pending``. The resume is
        auditable: the ``run_resumed`` event and the report name the restored
        checkpoints, the stored/requested config fingerprints and the new
        fencing token.

        S2-2: ``plan`` may be omitted when ``run_id`` is given -- plan and
        workspace are then rebuilt from the durable store (``run_plans``
        table), so a restart never depends on process-in-memory plan maps.
        """
        if plan is None:
            if run_id is None:
                raise DurableError("resume requires a plan or a run_id to load one")
            plan, stored_workspace = self.checkpoints.load_plan(run_id)
            if workspace is None:
                workspace = stored_workspace
        run_id = str(plan.run_id)
        record = self.checkpoints.get_run(run_id)
        fingerprint = config_fingerprint(plan, self.registry)
        if fingerprint != record.config_fingerprint and not allow_config_mismatch:
            raise CheckpointMismatchError(run_id, record.config_fingerprint, fingerprint)
        if workspace is None:
            workspace = record.workspace

        restored = self.checkpoints.load_checkpoints(run_id)
        restored_steps = sorted(s for s, cp in restored.items() if cp.state == StepState.SUCCEEDED)
        graph = StateGraph.restore(plan, {s: cp.state for s, cp in restored.items()})

        if record.state in TERMINAL_RUN_STATES:
            results = {
                s: ToolResult(
                    status=ToolStatus.SUCCESS,
                    output=restored[s].output or {},
                    artifacts=list(restored[s].artifacts),
                )
                for s in restored_steps
            }
            return DurableRunReport(
                run_id=run_id,
                status=record.state,
                step_states=graph.states(),
                results=results,
                attempts={s: restored[s].attempt for s in restored_steps},
                tools_executed=0,
                resume=ResumeInfo(
                    resumed=True,
                    restored_steps=restored_steps,
                    config_fingerprint=fingerprint,
                    stored_config_fingerprint=record.config_fingerprint,
                    owner=record.owner,
                    fencing_token=0,
                ),
                reason="run_already_terminal",
            )

        new_owner = owner or record.owner or self.owner
        previous = self.leases.current(run_id)
        lease = self.leases.acquire(run_id, new_owner, self.lease_ttl_s, force=force_takeover)
        self._set_run_state(run_id, record.state, RunState.RUNNING)
        token = self.cancel_token(run_id)
        takeover = previous is not None and previous.owner != lease.owner
        self.events.append(
            run_id,
            EventType.RUN_RESUMED,
            {
                "restored_steps": restored_steps,
                "from_checkpoints": {s: restored[s].attempt for s in restored_steps},
                "config_fingerprint": fingerprint,
                "stored_config_fingerprint": record.config_fingerprint,
                "owner": lease.owner,
                "fencing_token": lease.fencing_token,
            },
        )
        self.events.append(
            run_id,
            EventType.LEASE_TAKEN_OVER if takeover else EventType.LEASE_ACQUIRED,
            {"owner": lease.owner, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at},
        )
        resume_info = ResumeInfo(
            resumed=True,
            restored_steps=restored_steps,
            config_fingerprint=fingerprint,
            stored_config_fingerprint=record.config_fingerprint,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
        )
        return self._execute(plan, graph, workspace, lease, token, restored=restored, resume_info=resume_info)

    # ------------------------------------------------------------------
    # execution loop
    # ------------------------------------------------------------------

    def _execute(
        self,
        plan: ExecutionPlan,
        graph: StateGraph,
        workspace: str,
        lease: Lease,
        token: CancelToken,
        *,
        restored: dict[str, Checkpoint],
        resume_info: ResumeInfo,
    ) -> DurableRunReport:
        run_id = str(plan.run_id)
        results: dict[str, ToolResult] = {}
        attempts: dict[str, int] = {}
        tools_executed = 0

        try:
            for step_id in graph.topological_order():
                step = graph.step(step_id)
                cp = restored.get(step_id)
                if cp is not None and cp.state == StepState.SUCCEEDED:
                    results[step_id] = ToolResult(
                        status=ToolStatus.SUCCESS,
                        output=cp.output or {},
                        artifacts=list(cp.artifacts),
                    )
                    attempts[step_id] = cp.attempt
                    self.events.append(
                        run_id,
                        EventType.NODE_SKIPPED,
                        {"step_id": step_id, "reason": "checkpoint_hit", "checkpoint_attempt": cp.attempt},
                    )
                    continue

                if token.cancelled or self.checkpoints.cancel_requested(run_id):
                    return self._finalize_cancel(run_id, graph, results, attempts, tools_executed, resume_info)

                if self.require_review is not None and self.require_review(step):
                    graph.transition(step_id, StepState.WAITING_REVIEW)
                    self.events.append(
                        run_id,
                        EventType.WAITING_REVIEW,
                        {"step_id": step_id, "tool": step.tool, "risk": step.risk.value},
                    )
                    current = self.checkpoints.get_run(run_id).state
                    self._set_run_state(run_id, current, RunState.WAITING_REVIEW)
                    return DurableRunReport(
                        run_id=run_id,
                        status=RunState.WAITING_REVIEW,
                        step_states=graph.states(),
                        results=results,
                        attempts=attempts,
                        tools_executed=tools_executed,
                        resume=resume_info,
                        reason=f"waiting_review:{step_id}",
                    )

                # Fencing: validate before the node *and* again after the
                # hook, so a takeover observed by the hook still blocks the
                # stale owner's tool call.
                lease = self._renew_lease(run_id, lease)
                self.leases.validate(run_id, lease.owner, lease.fencing_token)
                if self.before_node is not None:
                    self.before_node(run_id, step_id)
                lease = self._renew_lease(run_id, lease)
                self.leases.validate(run_id, lease.owner, lease.fencing_token)
                if token.cancelled or self.checkpoints.cancel_requested(run_id):
                    return self._finalize_cancel(run_id, graph, results, attempts, tools_executed, resume_info)

                result, n_attempts = self._execute_step(
                    step, graph, run_id, workspace, token, lease, fingerprint=resume_info.config_fingerprint
                )
                results[step_id] = result
                attempts[step_id] = n_attempts
                tools_executed += 1

                if result.status != ToolStatus.SUCCESS:
                    current = self.checkpoints.get_run(run_id).state
                    self._set_run_state(run_id, current, RunState.FAILED)
                    self.events.append(
                        run_id,
                        EventType.RUN_FAILED,
                        {
                            "step_id": step_id,
                            "status": result.status.value,
                            "code": result.error.code if result.error else "",
                        },
                    )
                    return DurableRunReport(
                        run_id=run_id,
                        status=RunState.FAILED,
                        step_states=graph.states(),
                        results=results,
                        attempts=attempts,
                        tools_executed=tools_executed,
                        resume=resume_info,
                        reason=result.error.code if result.error else "step_failed",
                    )

            current = self.checkpoints.get_run(run_id).state
            lease = self._renew_lease(run_id, lease)
            self.leases.validate(run_id, lease.owner, lease.fencing_token)
            self._set_run_state(run_id, current, RunState.SUCCEEDED)
            self.events.append(
                run_id,
                EventType.RUN_COMPLETED,
                {"tools_executed": tools_executed, "resumed": resume_info.resumed},
            )
            return DurableRunReport(
                run_id=run_id,
                status=RunState.SUCCEEDED,
                step_states=graph.states(),
                results=results,
                attempts=attempts,
                tools_executed=tools_executed,
                resume=resume_info,
            )
        except _RunCancelledSignal:
            return self._finalize_cancel(run_id, graph, results, attempts, tools_executed, resume_info)
        except LeaseFencingError as exc:
            # Fenced writer: do not write anything else to this run -- the
            # new owner is the only writer now.
            record = self.checkpoints.get_run(run_id)
            return DurableRunReport(
                run_id=run_id,
                status=record.state,
                step_states=graph.states(),
                results=results,
                attempts=attempts,
                tools_executed=tools_executed,
                resume=resume_info,
                reason=f"lease_lost:{exc}",
                fenced=True,
            )

    def _execute_step(
        self,
        step: PlanStep,
        graph: StateGraph,
        run_id: str,
        workspace: str,
        token: CancelToken,
        lease: Lease,
        *,
        fingerprint: str,
    ) -> tuple[ToolResult, int]:
        step_id = step.step_id
        graph.transition(step_id, StepState.RUNNING)
        self._save_step(run_id, step_id, StepState.RUNNING, attempt=1, fingerprint=fingerprint)
        spec = self.registry.get(step.tool)
        has_bindings = any(is_reference(v) for v in step.arguments.values())
        self.events.append(
            run_id,
            EventType.NODE_STARTED,
            {
                "step_id": step_id,
                "tool": step.tool,
                "resolved_arg_keys": sorted(
                    k for k, v in step.arguments.items() if is_reference(v)
                ),
            },
        )

        attempt = 0
        while True:
            attempt += 1
            if attempt > 1:
                graph.transition(step_id, StepState.RUNNING)  # failed -> running (retry re-entry)
                self._save_step(run_id, step_id, StepState.RUNNING, attempt=attempt, fingerprint=fingerprint)
            if token.cancelled:
                raise _RunCancelledSignal()
            result = self._resolve_and_invoke(step, spec, run_id, workspace, token, has_bindings)
            if result.error is not None and result.error.code == "run.cancelled":
                raise _RunCancelledSignal()

            # S2-6: the tool ran while the lease may have been taken over.
            # A stale owner must not commit checkpoints/events: fence BEFORE
            # any state mutation below. Renew first so a merely-long tool
            # call does not fence its own rightful owner; a genuine takeover
            # still fails the renew-acquire.
            lease = self._renew_lease(run_id, lease)
            self.leases.validate(run_id, lease.owner, lease.fencing_token)

            if result.status == ToolStatus.SUCCESS:
                graph.transition(step_id, StepState.SUCCEEDED)
                self._save_step(
                    run_id,
                    step_id,
                    StepState.SUCCEEDED,
                    attempt=attempt,
                    output=result.output,
                    artifacts=result.artifacts,
                    fingerprint=fingerprint,
                )
                self.events.append(
                    run_id,
                    EventType.NODE_COMPLETED,
                    {"step_id": step_id, "attempt": attempt, "latency_ms": result.metrics.latency_ms},
                )
                return result, attempt

            graph.transition(step_id, StepState.FAILED)
            error = result.error.model_dump() if result.error else None
            self._save_step(
                run_id, step_id, StepState.FAILED, attempt=attempt, error=error, fingerprint=fingerprint
            )
            decision = self.backoff.decide(
                result, attempt, policy_allows=step.retry_policy == RetryPolicy.TRANSIENT_ONLY
            )
            if decision.retry:
                self.events.append(
                    run_id,
                    EventType.NODE_RETRIED,
                    {
                        "step_id": step_id,
                        "attempt": attempt,
                        "delay_ms": decision.delay_ms,
                        "code": result.error.code if result.error else "",
                    },
                )
                self.backoff.wait(attempt)
                continue
            self.events.append(
                run_id,
                EventType.NODE_FAILED,
                {
                    "step_id": step_id,
                    "attempts": attempt,
                    "status": result.status.value,
                    "code": result.error.code if result.error else "",
                    "classification": self.backoff.classify(result),
                    "no_retry_reason": decision.reason,
                },
            )
            return result, attempt

    def _resolve_and_invoke(
        self,
        step: PlanStep,
        spec,
        run_id: str,
        workspace: str,
        token: CancelToken,
        has_bindings: bool,
    ) -> ToolResult:
        """Resolve step-output references (S1-7B) then invoke the tool.

        Resolution reads only *succeeded* checkpoint outputs from the durable
        store, so in-process runs, retries, and cross-process resumes all use
        identical inputs. Any resolution or re-validation failure is a typed
        TERMINAL_ERROR and the tool handler is never called.
        """
        arguments = step.arguments
        if has_bindings:
            if spec is None:
                return ToolResult(
                    status=ToolStatus.TERMINAL_ERROR,
                    error=ToolError(
                        code=ReasonCode.GUARD_UNKNOWN_TOOL.value,
                        message=f"unknown tool {step.tool}",
                        retryable=False,
                    ),
                    metrics=ToolMetrics(latency_ms=0, token_count=0),
                )
            try:
                sources = {
                    cid: cp.output or {}
                    for cid, cp in self.checkpoints.load_checkpoints(run_id).items()
                    if cp.state == StepState.SUCCEEDED
                }
                arguments, trace_records = resolve_step_arguments(
                    step, spec.arguments_schema, sources
                )
                for record in trace_records:
                    self.events.append(run_id, EventType.BINDING_RESOLVED, record)
                schema_codes = validate_arguments(arguments, spec.arguments_schema)
                if schema_codes:
                    return ToolResult(
                        status=ToolStatus.TERMINAL_ERROR,
                        error=ToolError(
                            code=schema_codes[0],
                            message="resolved arguments failed tool schema validation",
                            retryable=False,
                        ),
                        metrics=ToolMetrics(latency_ms=0, token_count=0),
                    )
            except BindingResolutionError as exc:
                return ToolResult(
                    status=ToolStatus.TERMINAL_ERROR,
                    error=ToolError(code=exc.code, message=exc.message, retryable=False),
                    metrics=ToolMetrics(latency_ms=0, token_count=0),
                )
        return self._invoke_isolated(step, run_id, workspace, token, arguments)

    def _invoke_isolated(
        self, step: PlanStep, run_id: str, workspace: str, token: CancelToken, arguments: dict
    ) -> ToolResult:
        """Invoke the tool in a daemon thread so a hung node can be bounded
        by ``timeout_ms`` and interrupted by the cancel token. Tool exceptions
        (including simulated crashes) are re-raised in the runner thread."""
        holder: dict[str, Any] = {}

        def target() -> None:
            try:
                holder["result"] = self.registry.invoke(
                    step.tool, arguments, run_id=run_id, workspace=workspace
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                holder["exc"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        deadline = time.monotonic() + step.timeout_ms / 1000.0
        while thread.is_alive():
            if token.cancelled:
                return ToolResult(
                    status=ToolStatus.DENIED,
                    error=ToolError(code="run.cancelled", message="run cancelled while node executing", retryable=False),
                    metrics=ToolMetrics(latency_ms=0, token_count=0),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ToolResult(
                    status=ToolStatus.RETRYABLE_ERROR,
                    error=ToolError(
                        code="node.timeout",
                        message=f"node {step.step_id} exceeded timeout_ms={step.timeout_ms}",
                        retryable=True,
                    ),
                    metrics=ToolMetrics(latency_ms=step.timeout_ms, token_count=0),
                )
            thread.join(min(0.005, remaining))
        if "exc" in holder:
            raise holder["exc"]
        return holder["result"]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _renew_lease(self, run_id: str, lease: Lease) -> Lease:
        """Renew the lease when it is within half a TTL of expiring (long tool
        calls routinely outlive lease_ttl_s). Same-owner acquire bumps the
        fencing token; a different live owner raises LeaseConflictError,
        normalized here into the fencing signal every commit path handles.
        Far-from-expiry renewals are skipped so short runs keep a stable
        single fencing token."""
        if lease.expires_at - self.leases._now() > self.lease_ttl_s / 2:
            return lease
        try:
            return self.leases.acquire(run_id, self.owner, self.lease_ttl_s)
        except LeaseConflictError as exc:
            raise LeaseFencingError(run_id, self.owner, f"renew blocked: {exc}") from exc

    def _save_step(
        self,
        run_id: str,
        step_id: str,
        state: StepState,
        *,
        attempt: int,
        output: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
        error: dict[str, Any] | None = None,
        fingerprint: str,
    ) -> None:
        self.checkpoints.save_checkpoint(
            Checkpoint(
                run_id=run_id,
                step_id=step_id,
                state=state,
                attempt=attempt,
                output=output,
                artifacts=list(artifacts or []),
                error=error,
                config_fingerprint=fingerprint,
                created_at=self.checkpoints._now(),
            )
        )

    def _set_run_state(self, run_id: str, from_state: RunState, to_state: RunState) -> None:
        guard_run_transition(from_state, to_state)
        self.checkpoints.set_run_state(run_id, to_state)

    def _finalize_cancel(
        self,
        run_id: str,
        graph: StateGraph,
        results: dict[str, ToolResult],
        attempts: dict[str, int],
        tools_executed: int,
        resume_info: ResumeInfo,
    ) -> DurableRunReport:
        cancelled: list[str] = []
        for step_id in graph.unfinished_steps():
            graph.transition(step_id, StepState.CANCELLED)
            cancelled.append(step_id)
        current = self.checkpoints.get_run(run_id).state
        if current not in TERMINAL_RUN_STATES:
            self._set_run_state(run_id, current, RunState.CANCELLED)
        self.events.append(run_id, EventType.RUN_CANCELLED, {"cancelled_steps": cancelled})
        return DurableRunReport(
            run_id=run_id,
            status=RunState.CANCELLED,
            step_states=graph.states(),
            results=results,
            attempts=attempts,
            tools_executed=tools_executed,
            resume=resume_info,
            reason="cancelled",
        )
