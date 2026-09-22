"""Scenario (b): the user corrects an old preference -> supersede. The old
value stays auditable but is not recalled by default; history is never
overwritten."""

from __future__ import annotations

import pytest

from app.memory import ReadQuery, RecordStatus, attempt_write, supersede
from app.memory.models import MemoryRecord

from conftest import make_candidate


def test_supersede_marks_old_and_recalls_new(store, gate, read_gate):
    old = attempt_write(store, gate, make_candidate(value="vim")).record

    corrected = make_candidate(value="emacs", write_reason="用户纠正旧偏好", user_confirmation_id="confirm-2")
    decision = supersede(store, gate, old.memory_id, corrected)
    assert decision.allowed, decision.reasons

    old_after = store.get(old.memory_id)
    assert old_after.status == RecordStatus.SUPERSEDED
    assert old_after.superseded_by == decision.record.memory_id
    assert old_after.value == "vim"  # history not overwritten

    results = read_gate.recall(store, ReadQuery(workspace="ws-a", text="偏好编辑器"))
    assert [r.value for r in results] == ["emacs"]


def test_superseded_value_not_recalled_but_auditable(store, gate, read_gate):
    old = attempt_write(store, gate, make_candidate(value="vim")).record
    supersede(store, gate, old.memory_id, make_candidate(value="emacs", user_confirmation_id="confirm-2"))

    # default recall: old value absent
    results = read_gate.recall(store, ReadQuery(workspace="ws-a"))
    assert old.memory_id not in {r.memory_id for r in results}

    # audit path: still queryable with full history
    assert store.get(old.memory_id).value == "vim"
    all_records = store.list_records(workspace="ws-a", include_superseded=True)
    assert {r.value for r in all_records} == {"vim", "emacs"}

    trail = store.audit_trail(old.memory_id)
    actions = [e["action"] for e in trail]
    assert actions == ["write", "supersede"]
    snapshot = MemoryRecord.model_validate_json(trail[-1]["snapshot_json"])
    assert snapshot.value == "vim"


def test_supersede_requires_gate_pass(store, gate):
    """An unconfirmed user_fact correction is still rejected by the gate."""
    old = attempt_write(store, gate, make_candidate(value="vim")).record
    bad = make_candidate(value="emacs", user_confirmed=False)
    decision = supersede(store, gate, old.memory_id, bad)
    assert not decision.allowed
    assert "missing_user_confirmation" in decision.reasons
    assert store.get(old.memory_id).status == RecordStatus.ACTIVE


def test_supersede_target_mismatch_rejected(store, gate):
    old = attempt_write(store, gate, make_candidate(value="vim", subject="偏好编辑器")).record
    wrong_subject = make_candidate(value="emacs", subject="偏好主题", user_confirmation_id="confirm-2")
    decision = supersede(store, gate, old.memory_id, wrong_subject)
    assert not decision.allowed
    assert "supersede_target_mismatch" in decision.reasons


def test_supersede_unknown_target_raises(store, gate):
    with pytest.raises(KeyError):
        supersede(store, gate, "mem-does-not-exist", make_candidate())
