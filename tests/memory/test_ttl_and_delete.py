"""Scenario (e): expired decisions must not be recalled -- TTL and valid_to
are enforced, the stale-hit rate of default recall is measurable and 0, and
TTL cleanup truly removes expired records (with audit)."""

from __future__ import annotations

import pytest

from app.memory import ReadQuery, attempt_write
from app.memory.metrics import stale_hit_rate
from app.memory.models import MemoryCategory

from conftest import make_candidate


def _write_decision(store, gate, **overrides):
    base = dict(
        category=MemoryCategory.DECISION,
        subject="临时部署决策",
        value="本周先用单机部署",
    )
    base.update(overrides)
    cand = make_candidate(**base)
    decision = attempt_write(store, gate, cand)
    assert decision.allowed, decision.reasons
    return decision.record


def test_ttl_expired_decision_not_recalled(store, gate, read_gate, clock):
    _write_decision(store, gate, ttl_seconds=3600)

    fresh = read_gate.recall(store, ReadQuery(workspace="ws-a", text="部署决策"))
    assert len(fresh) == 1
    assert stale_hit_rate(fresh, clock.now()) == 0.0

    clock.advance(7200)
    stale_results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="部署决策"))
    assert stale_results == []
    assert stale_hit_rate(stale_results, clock.now()) == 0.0


def test_valid_to_expired_decision_not_recalled(store, gate, read_gate, clock):
    _write_decision(store, gate, valid_to=clock.now() + 100)
    clock.advance(200)
    assert read_gate.recall(store, ReadQuery(workspace="ws-a", text="部署决策")) == []


def test_expired_record_still_auditable_via_store(store, gate, read_gate, clock):
    record = _write_decision(store, gate, ttl_seconds=10)
    clock.advance(20)
    assert read_gate.recall(store, ReadQuery(workspace="ws-a")) == []
    assert store.get(record.memory_id).value == "本周先用单机部署"


def test_ttl_cleanup_removes_and_audits(store, gate, clock):
    keep = _write_decision(store, gate, subject="长期决策", value="一直有效")
    expired = _write_decision(store, gate, ttl_seconds=60)
    clock.advance(120)

    removed = store.cleanup_expired()
    assert removed == [expired.memory_id]
    assert store.get(keep.memory_id).memory_id == keep.memory_id
    with pytest.raises(KeyError):
        store.get(expired.memory_id)

    trail = store.audit_trail(expired.memory_id)
    assert [e["action"] for e in trail] == ["write", "ttl_cleanup"]


def test_cleanup_preserves_superseded_history(store, gate, clock):
    """Superseded records are historical: TTL cleanup must not touch them."""
    from app.memory import supersede

    old = _write_decision(store, gate, ttl_seconds=60)
    new = make_candidate(
        category=MemoryCategory.DECISION,
        subject="临时部署决策",
        value="改为集群部署",
        user_confirmation_id="confirm-2",
    )
    supersede(store, gate, old.memory_id, new)
    clock.advance(120)
    removed = store.cleanup_expired()
    assert removed == []
    assert store.get(old.memory_id).value == "本周先用单机部署"


def test_explicit_delete_true_removal_with_audit(store, gate, read_gate):
    record = _write_decision(store, gate)
    store.delete(record.memory_id, reason="用户要求删除", actor="user")
    with pytest.raises(KeyError):
        store.get(record.memory_id)
    assert read_gate.recall(store, ReadQuery(workspace="ws-a")) == []

    trail = store.audit_trail(record.memory_id)
    assert [e["action"] for e in trail] == ["write", "delete"]
    assert trail[-1]["reason"] == "用户要求删除"
    assert "本周先用单机部署" in trail[-1]["snapshot_json"]  # snapshot preserved
