"""Frozen benchmark cases from benchmarks/memory_cases.jsonl, parametrized.

Each case drives the full gate stack (Write Gate / Read Gate / supersede /
delete) against a fresh SQLite store with a deterministic clock, and asserts
the frozen expectation (outcome, reasons, recalled values, audit actions,
leakage, stale hits).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.memory import MemoryStore, ReadGate, ReadQuery, WriteGate, attempt_write, supersede
from app.memory.metrics import cross_workspace_leakage, stale_hit_rate
from app.memory.models import MemoryCandidate

from conftest import FakeClock, case_ids, load_cases

CASES = load_cases("memory_cases.jsonl")


def test_benchmark_has_at_least_12_cases():
    assert len(CASES) >= 12


def _recall_all(store, read_gate, workspace: str):
    return read_gate.recall(store, ReadQuery(workspace=workspace, top_k=100))


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_memory_case(tmp_path: Path, case: dict):
    clock = FakeClock()
    store = MemoryStore(str(tmp_path / f"{case['case_id']}.db"), now=clock.now)
    gate = WriteGate(now=clock.now)
    read_gate = ReadGate(now=clock.now)
    expected = case["expected"]
    try:
        seeded = []
        for seed in case.get("seed", []):
            decision = attempt_write(store, gate, MemoryCandidate.model_validate(seed))
            assert decision.allowed, f"seed write unexpectedly rejected: {decision.reasons}"
            seeded.append(decision.record)

        clock.advance(case.get("advance_seconds", 0))
        action = case["action"]

        if action == "write":
            decision = gate.evaluate(MemoryCandidate.model_validate(case["candidate"]), store=store)
            assert decision.outcome.value == expected["outcome"]
            for reason in expected.get("reasons_include", []):
                assert reason in decision.reasons
            if "recalled_values" in expected:
                recalled = _recall_all(store, read_gate, case["candidate"]["workspace"])
                assert [r.value for r in recalled] == expected["recalled_values"]
                assert len(recalled) == expected.get("recall_count_after", len(recalled))

        elif action == "recall":
            query = ReadQuery(**case["query"])
            recalled = read_gate.recall(store, query)
            assert [r.value for r in recalled] == expected["recalled_values"]
            if "stale_hits" in expected:
                assert stale_hit_rate(recalled, clock.now()) == expected["stale_hits"]
            if "cross_workspace_leakage" in expected:
                assert cross_workspace_leakage(recalled, query.workspace) == expected["cross_workspace_leakage"]

        elif action == "supersede":
            decision = supersede(
                store, gate, seeded[0].memory_id, MemoryCandidate.model_validate(case["candidate"])
            )
            assert decision.outcome.value == expected["outcome"]
            recalled = _recall_all(store, read_gate, seeded[0].workspace)
            assert [r.value for r in recalled] == expected["recalled_values"]
            assert store.get(seeded[0].memory_id).status.value == expected["old_status"]
            actions = [e["action"] for e in store.audit_trail()]
            assert actions == expected["audit_actions"]

        elif action == "delete":
            store.delete(seeded[0].memory_id, reason="用户要求删除", actor="user")
            recalled = _recall_all(store, read_gate, seeded[0].workspace)
            assert [r.value for r in recalled] == expected["recalled_values"]
            actions = [e["action"] for e in store.audit_trail(seeded[0].memory_id)]
            assert actions == expected["audit_actions"]

        else:  # pragma: no cover - guards against typos in the frozen file
            raise AssertionError(f"unknown action: {action}")
    finally:
        store.close()
