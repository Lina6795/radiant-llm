"""Scenario f: illegal state transitions raise typed errors; legal paths work."""

from __future__ import annotations

import pytest

from app.durable.errors import IllegalTransitionError
from app.durable.graph import (
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    RunState,
    StateGraph,
    StepState,
    guard_run_transition,
    guard_step_transition,
)

from tests.durable.conftest import make_plan, make_step
from tests.durable.scenarios import scenario_illegal_transition


def test_illegal_step_transition_raises_typed_error() -> None:
    plan = make_plan([make_step("s1", "evidence.search", arguments={"query": "x"})])
    graph = StateGraph.from_plan(plan)
    with pytest.raises(IllegalTransitionError) as excinfo:
        graph.transition("s1", StepState.SUCCEEDED)  # pending -> succeeded
    assert excinfo.value.entity == "step"
    assert excinfo.value.from_state == "pending"
    assert excinfo.value.to_state == "succeeded"


def test_terminal_states_reject_every_transition() -> None:
    for terminal in (StepState.SUCCEEDED, StepState.CANCELLED):
        for target in StepState:
            with pytest.raises(IllegalTransitionError):
                guard_step_transition(terminal, target)
    for terminal in (RunState.SUCCEEDED, RunState.CANCELLED):
        for target in RunState:
            with pytest.raises(IllegalTransitionError):
                guard_run_transition(terminal, target)


def test_transition_tables_are_internally_consistent() -> None:
    for state, targets in STEP_TRANSITIONS.items():
        assert state in StepState
        assert targets <= frozenset(StepState)
    for state, targets in RUN_TRANSITIONS.items():
        assert state in RunState
        assert targets <= frozenset(RunState)


def test_legal_retry_and_cancel_paths() -> None:
    plan = make_plan([make_step("s1", "evidence.search", arguments={"query": "x"})])
    graph = StateGraph.from_plan(plan)
    graph.transition("s1", StepState.RUNNING)
    graph.transition("s1", StepState.FAILED)
    graph.transition("s1", StepState.RUNNING)  # retry re-entry
    graph.transition("s1", StepState.FAILED)
    graph.transition("s1", StepState.CANCELLED)  # cancel between retries
    assert graph.state("s1") == StepState.CANCELLED


def test_restore_normalizes_inflight_states_to_pending() -> None:
    plan = make_plan(
        [make_step("s1", "t"), make_step("s2", "t", depends_on=["s1"]), make_step("s3", "t", depends_on=["s2"])]
    )
    graph = StateGraph.restore(
        plan,
        {"s1": StepState.SUCCEEDED, "s2": StepState.RUNNING, "s3": StepState.PENDING},
    )
    # Only succeeded survives reconstruction; a dead worker's running step
    # must be re-executable, not skipped.
    assert graph.state("s1") == StepState.SUCCEEDED
    assert graph.state("s2") == StepState.PENDING
    assert graph.state("s3") == StepState.PENDING


def test_run_resume_edge_is_explicit() -> None:
    guard_run_transition(RunState.RUNNING, RunState.RUNNING)  # resume re-entry
    guard_run_transition(RunState.FAILED, RunState.RUNNING)  # resume after failure
    guard_run_transition(RunState.WAITING_REVIEW, RunState.RUNNING)  # approved review


def test_scenario_driver_f(tmp_path) -> None:
    detail = scenario_illegal_transition(tmp_path)
    assert detail["typed_error_raised"] is True
    assert detail["error_type"] == "IllegalTransitionError"
    assert detail["legal_path_ok"] is True
