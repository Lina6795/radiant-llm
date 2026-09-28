"""S2-7: persistent idempotency for a controlled write operation.

Restart + same idempotency key must not repeat the side effect (the ledger
replays the recorded outcome), and read-only runs must never be counted as
write-idempotency proof.
"""

from __future__ import annotations

import uuid

from app.control.models import Risk
from app.control.registry import build_default_registry
from app.durable.graph import RunState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, make_plan, make_step, reopen_stores


def _runner(stores: Stores) -> DurableRunner:
    registry = build_default_registry()
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger,
    )


def _export_plan(key: str):
    return make_plan(
        [
            make_step(
                "export", "report.export",
                arguments={"title": "t", "content": "c", "idempotency_key": key},
                risk=Risk.BOUNDED_WRITE, idempotency_key=key,
            )
        ],
        run_id=uuid.uuid4(),  # duplicate submission: new run, SAME key
    )


def test_restart_same_key_no_duplicate_effect(tmp_path) -> None:
    key = "k-restart"
    stores = Stores(tmp_path / "idem.db")
    first = _runner(stores).run(_export_plan(key))
    assert first.status == RunState.SUCCEEDED
    assert stores.ledger.effect_count_for(key) == 1

    # simulated process restart: brand-new store instances + new runner
    stores2 = reopen_stores(stores)
    second = _runner(stores2).run(_export_plan(key))
    assert second.status == RunState.SUCCEEDED
    assert stores2.ledger.effect_count_for(key) == 1  # effect NOT repeated
    replayed = stores2.ledger.get(key)
    assert replayed["artifact"].startswith("report://")


def test_read_only_run_leaves_ledger_empty(tmp_path) -> None:
    stores = Stores(tmp_path / "ro.db")
    plan = make_plan(
        [make_step("s1", "evidence.search", arguments={"query": "uranium half-life"})]
    )
    report = _runner(stores).run(plan)
    assert report.status == RunState.SUCCEEDED
    # read-only work is never deduplicated through the write ledger
    assert stores.ledger.effect_count == 0
