"""Scenario (a): two workspaces hold same-named entities; cross-workspace
leakage on recall must be exactly 0 (frozen isolation)."""

from __future__ import annotations

from app.memory import ReadQuery, attempt_write
from app.memory.metrics import cross_workspace_leakage
from app.memory.models import MemoryCategory

from conftest import make_candidate


def _seed_both_workspaces(store, gate):
    for ws, value in (("ws-a", "Alice 是前端负责人"), ("ws-b", "Alice 是合规官")):
        decision = attempt_write(
            store,
            gate,
            make_candidate(
                subject="Alice 的角色",
                value=value,
                workspace=ws,
                confidence=0.95,
            ),
        )
        assert decision.allowed, decision.reasons


def test_same_entity_two_workspaces_zero_leakage(store, gate, read_gate):
    _seed_both_workspaces(store, gate)

    for ws, expected in (("ws-a", "Alice 是前端负责人"), ("ws-b", "Alice 是合规官")):
        results = read_gate.recall(store, ReadQuery(workspace=ws, text="Alice 的角色"))
        assert len(results) == 1
        assert results[0].value == expected
        assert cross_workspace_leakage(results, ws) == 0


def test_cross_workspace_leakage_metric_is_zero(store, gate, read_gate):
    _seed_both_workspaces(store, gate)
    for ws in ("ws-a", "ws-b"):
        results = read_gate.recall(store, ReadQuery(workspace=ws))
        assert cross_workspace_leakage(results, ws) == 0
        assert all(r.workspace == ws for r in results)


def test_unfiltered_recall_stays_in_workspace(store, gate, read_gate):
    _seed_both_workspaces(store, gate)
    results_a = read_gate.recall(store, ReadQuery(workspace="ws-a", top_k=50))
    assert {r.workspace for r in results_a} == {"ws-a"}


def test_write_in_one_workspace_does_not_conflict_with_other(store, gate, read_gate):
    """Same subject+same category in ws-b must not trigger conflict for ws-a."""
    _seed_both_workspaces(store, gate)
    decision = attempt_write(
        store,
        gate,
        make_candidate(subject="新主题", value="仅 ws-a", workspace="ws-a"),
    )
    assert decision.allowed
    results = read_gate.recall(store, ReadQuery(workspace="ws-b", text="新主题"))
    assert results == []
