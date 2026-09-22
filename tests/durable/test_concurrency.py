"""Scenario h: concurrent runs (threads) -- event streams and checkpoints of
different runs never interleave or leak into each other."""

from __future__ import annotations

import threading
import time
import uuid

from app.control.registry import build_default_registry
from app.durable.graph import RunState, StepState
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    Stores,
    counting_handler,
    make_backoff,
    make_plan,
    make_step,
    register_test_tool,
)
from tests.durable.scenarios import scenario_concurrent_runs


def test_scenario_h_concurrent_runs_isolated(tmp_path) -> None:
    detail = scenario_concurrent_runs(tmp_path)
    assert detail["both_succeeded"] is True
    assert detail["seq_monotonic"] is True
    assert detail["no_cross_events"] is True
    assert detail["isolated_tool_calls"] is True


def test_concurrent_runs_state_and_ledger_isolation(tmp_path) -> None:
    stores = Stores(tmp_path / "conc.db")
    registry = build_default_registry()
    seen: list[tuple[str, str]] = []

    def rec(arguments, ctx):
        time.sleep(0.003)
        seen.append((ctx.run_id, arguments["tag"]))
        return counting_handler([], name="rec")(arguments, ctx)

    register_test_tool(registry, "test.rec", rec)
    runner = DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        backoff=make_backoff(stores.clock),
    )

    def build(tag: str):
        return make_plan(
            [
                make_step("n1", "test.rec", arguments={"tag": tag}),
                make_step("n2", "test.rec", depends_on=["n1"], arguments={"tag": tag}),
            ],
            run_id=uuid.uuid4(),
            goal=f"run {tag}",
        )

    plans = [build(t) for t in ("A", "B", "C", "D")]
    reports: dict[str, object] = {}

    def drive(plan):
        reports[plan.goal] = runner.run(plan)

    threads = [threading.Thread(target=drive, args=(p,)) for p in plans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    run_ids = {p.goal: str(p.run_id) for p in plans}
    for goal, rid in run_ids.items():
        report = reports[goal]
        assert report.status == RunState.SUCCEEDED
        # Event stream: contiguous per-run seq, single run_id.
        stream = stores.events.stream(rid)
        assert [e.seq for e in stream] == list(range(1, len(stream) + 1))
        assert {e.run_id for e in stream} == {rid}
        # Checkpoints: exactly this run's steps, all succeeded.
        checkpoints = stores.checkpoints.load_checkpoints(rid)
        assert set(checkpoints) == {"n1", "n2"}
        assert all(cp.state == StepState.SUCCEEDED for cp in checkpoints.values())
        # Run record belongs to this run alone.
        record = stores.checkpoints.get_run(rid)
        assert record.state == RunState.SUCCEEDED
        assert record.goal == goal
        # Lease: single owner, single fencing token generation.
        lease = stores.leases.current(rid)
        assert lease.owner == "worker-0"
        assert lease.fencing_token == 1
    # Tool calls carry the right run_id on both sides.
    for goal, rid in run_ids.items():
        tag = goal.split()[-1]
        assert sorted(t for r, t in seen if r == rid) == [tag, tag]
    stores.close()
