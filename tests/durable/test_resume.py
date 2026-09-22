"""Scenario d: crash and precise resume -- completed nodes are never
re-executed, and every resume names its checkpoints and configuration."""

from __future__ import annotations

import pytest

from app.control.models import Risk
from app.control.registry import build_default_registry
from app.durable.errors import CheckpointMismatchError, RunNotFoundError
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    SimulatedCrash,
    Stores,
    counting_handler,
    make_backoff,
    make_plan,
    make_step,
    register_test_tool,
    reopen_stores,
)
from tests.durable.scenarios import scenario_crash_resume


def _runner(stores: Stores, registry, **kwargs) -> DurableRunner:
    kwargs.setdefault("backoff", make_backoff(stores.clock))
    return DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        **kwargs,
    )


def test_scenario_d_crash_resume(tmp_path) -> None:
    detail = scenario_crash_resume(tmp_path)
    assert detail["crashed"] is True
    assert detail["run_status"] == "succeeded"
    assert detail["resumed"] is True
    assert detail["restored_steps"] == ["s1"]
    assert detail["s1_calls"] == 1  # completed node NOT re-executed
    assert detail["s2_calls"] == 2  # crashed attempt + re-execution
    assert detail["s3_calls"] == 1
    assert detail["config_reported"] is True
    assert detail["resume_event_names_checkpoint"] is True


def test_resume_report_names_checkpoint_and_config(tmp_path) -> None:
    stores = Stores(tmp_path / "prov.db")
    registry = build_default_registry()
    mode = {"crash": True}
    s2_calls: list[str] = []

    def crashy(arguments, ctx):
        s2_calls.append("s2")
        if mode["crash"]:
            raise SimulatedCrash()
        return counting_handler([], name="s2")(arguments, ctx)

    register_test_tool(registry, "test.crashy", crashy)
    plan = make_plan(
        [make_step("a", "evidence.search", arguments={"query": "q"}), make_step("b", "test.crashy", depends_on=["a"])]
    )
    with pytest.raises(SimulatedCrash):
        _runner(stores, registry).run(plan)

    stores2 = reopen_stores(stores)
    mode["crash"] = False
    report = _runner(stores2, registry).resume(plan)
    run_id = str(plan.run_id)

    assert report.resume.resumed is True
    assert report.resume.restored_steps == ["a"]
    assert report.resume.config_fingerprint == report.resume.stored_config_fingerprint
    resumed = next(e for e in stores2.events.stream(run_id) if e.type == EventType.RUN_RESUMED)
    assert resumed.payload["from_checkpoints"] == {"a": 1}
    assert resumed.payload["config_fingerprint"] == report.resume.config_fingerprint
    checkpoint = stores2.checkpoints.load_checkpoints(run_id)["a"]
    assert checkpoint.state == StepState.SUCCEEDED
    assert checkpoint.config_fingerprint == report.resume.config_fingerprint
    stores2.close()


def test_resume_unknown_run_fails_closed(tmp_path) -> None:
    stores = Stores(tmp_path / "unknown.db")
    registry = build_default_registry()
    plan = make_plan([make_step("a", "evidence.search", arguments={"query": "q"})])
    with pytest.raises(RunNotFoundError):
        _runner(stores, registry).resume(plan)
    stores.close()


def test_resume_refuses_config_mismatch(tmp_path) -> None:
    stores = Stores(tmp_path / "mismatch.db")
    registry = build_default_registry()
    plan = make_plan([make_step("a", "evidence.search", arguments={"query": "q"})])
    _runner(stores, registry).run(plan)

    other = make_plan(
        [make_step("a", "evidence.search", arguments={"query": "DIFFERENT"})],
        run_id=plan.run_id,
    )
    with pytest.raises(CheckpointMismatchError) as excinfo:
        _runner(stores, registry).resume(other)
    assert excinfo.value.stored != excinfo.value.requested

    report = _runner(stores, registry).resume(other, allow_config_mismatch=True)
    assert report.status == RunState.SUCCEEDED
    assert report.resume.stored_config_fingerprint != report.resume.config_fingerprint
    stores.close()


def test_resume_terminal_run_is_a_noop(tmp_path) -> None:
    stores = Stores(tmp_path / "terminal.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.t", counting_handler(calls, name="t"))
    plan = make_plan([make_step("a", "test.t")])
    runner = _runner(stores, registry)
    assert runner.run(plan).status == RunState.SUCCEEDED
    assert len(calls) == 1

    report = runner.resume(plan)
    assert report.status == RunState.SUCCEEDED
    assert report.reason == "run_already_terminal"
    assert report.tools_executed == 0
    assert len(calls) == 1  # nothing re-executed
    stores.close()


def test_waiting_review_then_resume_after_approval(tmp_path) -> None:
    stores = Stores(tmp_path / "review.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.risky", counting_handler(calls, name="risky"), risk=Risk.EXTERNAL)
    plan = make_plan(
        [
            make_step("safe", "evidence.search", arguments={"query": "q"}),
            make_step("risky", "test.risky", depends_on=["safe"], risk=Risk.EXTERNAL),
        ]
    )
    runner = _runner(stores, registry, require_review=lambda step: step.risk == Risk.EXTERNAL)
    report = runner.run(plan)
    run_id = str(plan.run_id)
    assert report.status == RunState.WAITING_REVIEW
    assert report.step_states["risky"] == StepState.WAITING_REVIEW
    assert calls == []  # gated node never executed
    assert EventType.WAITING_REVIEW.value in [e.type.value for e in stores.events.stream(run_id)]

    approved = _runner(stores, registry, require_review=lambda step: False)
    report2 = approved.resume(plan)
    assert report2.status == RunState.SUCCEEDED
    assert calls == ["risky"]
    assert report2.resume.restored_steps == ["safe"]
    stores.close()
