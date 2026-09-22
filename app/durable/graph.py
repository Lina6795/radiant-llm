"""RADIANT-Control M3: explicit state graph for durable execution.

Self-contained replacement for LangGraph (no external dependency): nodes are
plan steps, edges come from ``depends_on``, and both step-level and run-level
state machines are governed by explicit legal-transition tables. Any
transition outside the table raises
:class:`app.durable.errors.IllegalTransitionError`.

Terminal step states: ``succeeded``, ``cancelled``.
Terminal run states: ``succeeded``, ``cancelled``.

The run-level self-edge ``running -> running`` is the *resume* edge: a run
whose worker died is still ``running`` in the store, and resuming re-enters
``running`` with an audit event rather than pretending a fresh start.
"""

from __future__ import annotations

from enum import Enum

from app.control.models import ExecutionPlan, PlanStep
from app.durable.errors import DurableError, IllegalTransitionError


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_REVIEW = "waiting_review"
    CANCELLED = "cancelled"


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_REVIEW = "waiting_review"
    CANCELLED = "cancelled"


STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING, StepState.WAITING_REVIEW, StepState.CANCELLED}),
    StepState.RUNNING: frozenset(
        {StepState.SUCCEEDED, StepState.FAILED, StepState.WAITING_REVIEW, StepState.CANCELLED}
    ),
    # failed -> running is the retry re-entry edge; failed -> cancelled lets a
    # cancel land between the last failed attempt and the next retry.
    StepState.FAILED: frozenset({StepState.RUNNING, StepState.CANCELLED}),
    StepState.WAITING_REVIEW: frozenset({StepState.RUNNING, StepState.CANCELLED}),
    StepState.SUCCEEDED: frozenset(),
    StepState.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    # running -> running is the resume edge (see module docstring).
    RunState.RUNNING: frozenset(
        {RunState.RUNNING, RunState.SUCCEEDED, RunState.FAILED, RunState.WAITING_REVIEW, RunState.CANCELLED}
    ),
    RunState.WAITING_REVIEW: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    # failed -> running allows resuming a failed run from its checkpoints.
    RunState.FAILED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

TERMINAL_STEP_STATES = frozenset({StepState.SUCCEEDED, StepState.CANCELLED})
TERMINAL_RUN_STATES = frozenset({RunState.SUCCEEDED, RunState.CANCELLED})


def guard_step_transition(from_state: StepState, to_state: StepState) -> None:
    if to_state not in STEP_TRANSITIONS[from_state]:
        raise IllegalTransitionError("step", from_state.value, to_state.value)


def guard_run_transition(from_state: RunState, to_state: RunState) -> None:
    if to_state not in RUN_TRANSITIONS[from_state]:
        raise IllegalTransitionError("run", from_state.value, to_state.value)


class StateGraph:
    """Plan-step DAG with guarded per-node states.

    The graph only *guards* transitions; persistence (checkpoints, events) is
    the runner's job. ``restore`` reconstructs a graph from persisted states
    without going through the transition table -- rebuilding state from the
    store is not a transition, and stale in-flight states (``running`` /
    ``failed`` / ``waiting_review`` from a dead worker) are normalized back to
    ``pending`` so they can be legally re-entered.
    """

    def __init__(
        self,
        steps: list[PlanStep],
        initial_states: dict[str, StepState] | None = None,
    ) -> None:
        self._steps: dict[str, PlanStep] = {}
        self._states: dict[str, StepState] = {}
        for step in steps:
            if step.step_id in self._steps:
                raise DurableError(f"duplicate step_id: {step.step_id}")
            self._steps[step.step_id] = step
            self._states[step.step_id] = StepState.PENDING
        for dep_owner in steps:
            for dep in dep_owner.depends_on:
                if dep not in self._steps:
                    raise DurableError(f"unknown dependency {dep!r} of step {dep_owner.step_id!r}")
        if initial_states:
            for step_id, state in initial_states.items():
                if step_id in self._states:
                    self._states[step_id] = state
        self._order = self._topological_order()

    @classmethod
    def from_plan(cls, plan: ExecutionPlan) -> "StateGraph":
        return cls(plan.steps)

    @classmethod
    def restore(cls, plan: ExecutionPlan, persisted: dict[str, StepState]) -> "StateGraph":
        """Rebuild a graph from persisted step states for resume.

        Only ``succeeded`` states are preserved verbatim; anything else is
        normalized to ``pending`` because a persisted ``running`` step belongs
        to a worker that is no longer executing it.
        """
        initial = {
            step_id: StepState.SUCCEEDED
            for step_id, state in persisted.items()
            if state == StepState.SUCCEEDED
        }
        return cls(plan.steps, initial_states=initial)

    def _topological_order(self) -> list[str]:
        done: set[str] = set()
        ordered: list[str] = []
        remaining = list(self._steps)
        while remaining:
            progressed = False
            for step_id in list(remaining):
                if all(dep in done for dep in self._steps[step_id].depends_on):
                    ordered.append(step_id)
                    done.add(step_id)
                    remaining.remove(step_id)
                    progressed = True
            if not progressed:
                raise DurableError(f"cyclic dependency among steps: {sorted(remaining)}")
        return ordered

    def topological_order(self) -> list[str]:
        return list(self._order)

    def step(self, step_id: str) -> PlanStep:
        return self._steps[step_id]

    def state(self, step_id: str) -> StepState:
        return self._states[step_id]

    def states(self) -> dict[str, StepState]:
        return dict(self._states)

    def transition(self, step_id: str, to_state: StepState) -> StepState:
        from_state = self._states[step_id]
        guard_step_transition(from_state, to_state)
        self._states[step_id] = to_state
        return to_state

    def unfinished_steps(self) -> list[str]:
        return [s for s in self._order if self._states[s] not in TERMINAL_STEP_STATES]
