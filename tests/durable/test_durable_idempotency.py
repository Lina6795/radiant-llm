"""Scenarios c/e: persistent idempotency -- side effects happen exactly once
per key, across retries, duplicate submissions and process restarts."""

from __future__ import annotations

import threading

from app.control.models import Risk
from app.durable.graph import RunState

from tests.durable.conftest import Stores, make_plan, make_step
from tests.durable.scenarios import scenario_duplicate_idempotency_key, scenario_export_then_disconnect


def test_scenario_c_export_then_network_down(tmp_path) -> None:
    detail = scenario_export_then_disconnect(tmp_path)
    assert detail["first_run_crashed"] is True
    assert detail["resume_status"] == "succeeded"
    assert detail["replay_returned_first"] is True  # first result returned despite outage
    assert detail["effect_count"] == 1  # side effect did not run again
    assert detail["skipped_on_resume"] is True
    assert detail["export_tool_calls"] == 1  # export tool itself never re-invoked


def test_scenario_e_duplicate_submission(tmp_path) -> None:
    detail = scenario_duplicate_idempotency_key(tmp_path)
    assert detail["run_status"] == "succeeded"
    assert detail["effect_count"] == 1
    assert detail["replayed"] is True
    assert detail["same_artifact"] is True


def test_ledger_replays_across_instances(tmp_path) -> None:
    stores = Stores(tmp_path / "ledger.db")
    effects: list[str] = []

    def effect() -> dict:
        effects.append("ran")
        return {"n": len(effects)}

    first, replayed = stores.ledger.execute_once("k1", effect)
    assert (first, replayed) == ({"n": 1}, False)

    stores2 = Stores(tmp_path / "ledger.db", stores.clock)  # new instance, same file
    second, replayed2 = stores2.ledger.execute_once("k1", effect)
    assert (second, replayed2) == ({"n": 1}, True)
    assert len(effects) == 1
    assert stores2.ledger.effect_count == 1
    stores2.close()
    stores.close()


def test_ledger_is_thread_safe_for_one_key(tmp_path) -> None:
    stores = Stores(tmp_path / "threads.db")
    effects: list[str] = []

    def effect() -> dict:
        effects.append("ran")
        return {"n": len(effects)}

    results = []

    def submit():
        results.append(stores.ledger.execute_once("k-shared", effect))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(effects) == 1
    assert sum(1 for _r, replayed in results if not replayed) == 1
    assert all(r == {"n": 1} for r, _ in results)
    stores.close()


def test_report_export_full_path_uses_persistent_ledger(tmp_path) -> None:
    """The durable runner swaps the registry's M2 in-memory ledger for the
    persistent one (attribute injection on the instance, app/control untouched)."""
    from app.control.registry import build_default_registry
    from app.durable.runner import DurableRunner

    stores = Stores(tmp_path / "swap.db")
    registry = build_default_registry()
    runner = DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
    )
    assert registry.ledger is stores.ledger
    key = "k-swap"
    plan = make_plan(
        [
            make_step(
                "export",
                "report.export",
                arguments={"title": "t", "content": "c", "idempotency_key": key},
                risk=Risk.BOUNDED_WRITE,
                idempotency_key=key,
            )
        ]
    )
    report = runner.run(plan)
    assert report.status == RunState.SUCCEEDED
    assert stores.ledger.effect_count_for(key) == 1
    assert stores.ledger.get(key)["artifact"].startswith("report://")
    stores.close()
