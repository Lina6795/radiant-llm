"""Cancel semantics: cooperative cancellation between nodes and inside a
running node; after cancel no new tool calls are produced."""

from __future__ import annotations

import threading
import time

from app.control.registry import build_default_registry
from app.durable.events import EventType
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    Stores,
    counting_handler,
    make_backoff,
    make_plan,
    make_step,
    register_test_tool,
    spy_invocations,
)


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


def test_cancel_between_nodes_produces_no_new_tool_calls(tmp_path) -> None:
    stores = Stores(tmp_path / "cancel.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.t", counting_handler(calls, name="t"))
    invocations = spy_invocations(registry)
    plan = make_plan(
        [
            make_step("n1", "test.t"),
            make_step("n2", "test.t", depends_on=["n1"]),
            make_step("n3", "test.t", depends_on=["n2"]),
        ]
    )
    run_id = str(plan.run_id)
    holder: dict = {}

    def cancel_at_n2(rid: str, step_id: str) -> None:
        if step_id == "n2":
            holder["calls_at_cancel"] = len(invocations)
            holder["runner"].cancel(rid)

    runner = _runner(stores, registry, before_node=cancel_at_n2)
    holder["runner"] = runner
    report = runner.run(plan)

    assert report.status == RunState.CANCELLED
    assert holder["calls_at_cancel"] == 1
    assert len(invocations) == 1  # no new tool calls after cancel
    assert calls == ["t"]
    assert report.step_states["n1"] == StepState.SUCCEEDED
    assert report.step_states["n2"] == StepState.CANCELLED
    assert report.step_states["n3"] == StepState.CANCELLED
    types = [e.type.value for e in stores.events.stream(run_id)]
    assert types[-1] == EventType.RUN_CANCELLED.value
    assert "node_completed" not in types[types.index("run_cancelled"):]
    stores.close()


def test_cancel_token_interrupts_running_node(tmp_path) -> None:
    """Deterministic version: synchronization via threading.Event instead of
    wall-clock sleeps. Correctness must not depend on scheduling speed, so
    there is no elapsed-time assertion -- the proof of interruption is that
    the blocked tool observed the cancel token (exit_reasons == [True])."""
    stores = Stores(tmp_path / "token.db")
    registry = build_default_registry()
    calls: list[str] = []
    exit_reasons: list[bool] = []  # True == exited because the token fired
    node_started = threading.Event()
    holder: dict = {}

    def token_aware(arguments, ctx):
        calls.append("aware")
        token = holder["runner"].cancel_token(ctx.run_id)
        node_started.set()
        deadline = time.monotonic() + 30.0  # fallback only; never the pass condition
        while not token.cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
        exit_reasons.append(token.cancelled)
        return counting_handler([], name="aware")(arguments, ctx)

    register_test_tool(registry, "test.aware", token_aware)
    plan = make_plan(
        [make_step("n1", "test.aware", timeout_ms=60_000), make_step("n2", "test.aware", depends_on=["n1"])]
    )
    runner = _runner(stores, registry)
    holder["runner"] = runner
    run_id = str(plan.run_id)

    result: dict = {}

    def drive() -> None:
        result["report"] = runner.run(plan)

    thread = threading.Thread(target=drive)
    thread.start()
    assert node_started.wait(timeout=30.0)  # n1 is running; no fixed sleep guess
    runner.cancel(run_id)
    thread.join(timeout=30.0)

    assert not thread.is_alive()
    assert exit_reasons == [True]  # the running node was interrupted by the token
    report = result["report"]
    assert report.status == RunState.CANCELLED
    assert calls == ["aware"]  # n2 never started: no new tool calls after cancel
    assert report.step_states["n2"] == StepState.CANCELLED
    stores.close()


def test_cancel_persists_across_store_instances(tmp_path) -> None:
    """The persisted cancel flag is honoured by a different runner instance
    (e.g. a cancel issued while the worker was down)."""
    stores = Stores(tmp_path / "persist.db")
    registry = build_default_registry()
    calls: list[str] = []
    register_test_tool(registry, "test.t", counting_handler(calls, name="t"))
    plan = make_plan([make_step("n1", "test.t"), make_step("n2", "test.t", depends_on=["n1"])])
    run_id = str(plan.run_id)

    runner = _runner(stores, registry)
    stores.checkpoints.create_run(
        run_id, goal=plan.goal, workspace="default", owner="worker-0",
        config_fingerprint="fp-pending",
    )
    stores.checkpoints.request_cancel(run_id)

    from tests.durable.conftest import reopen_stores

    stores2 = reopen_stores(stores)
    runner2 = _runner(stores2, registry)
    report = runner2.resume(plan, allow_config_mismatch=True)
    assert report.status == RunState.CANCELLED
    assert calls == []  # cancelled before any node ran
    stores2.close()
