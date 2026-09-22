"""Filter unit tests: every drop records a reason."""

from retrieval.filters import FilterConfig, apply_filters
from retrieval.types import Candidate


def cand(eid, rank=1):
    return Candidate(evidence_id=eid, score=1.0, rank=rank, source="fused")


def by_id(sample_evidence):
    return {it["evidence_id"]: it for it in sample_evidence}


def test_workspace_filter_records_reason(sample_evidence):
    kept, dropped = apply_filters(
        [cand("ev-a1"), cand("ev-b1")], by_id(sample_evidence),
        FilterConfig(workspace_id="default"))
    assert [c.evidence_id for c in kept] == ["ev-a1"]
    assert dropped[0]["evidence_id"] == "ev-b1"
    assert any("workspace_mismatch" in r for r in dropped[0]["reasons"])


def test_degraded_excluded_by_default(sample_evidence):
    kept, dropped = apply_filters(
        [cand("ev-d1")], by_id(sample_evidence), FilterConfig())
    assert not kept
    assert any("degraded" in r for r in dropped[0]["reasons"])


def test_authority_and_modality_filters(sample_evidence):
    ev = by_id(sample_evidence)
    kept, dropped = apply_filters(
        [cand("ev-a1"), cand("ev-b1")], ev,
        FilterConfig(allowed_authority_levels=["primary"]))
    assert [c.evidence_id for c in kept] == ["ev-a1"]
    kept2, dropped2 = apply_filters(
        [cand("ev-a1")], ev, FilterConfig(allowed_modalities=["figure"]))
    assert not kept2
    assert any("modality_excluded" in r for r in dropped2[0]["reasons"])


def test_validity_window(sample_evidence):
    ev = by_id(sample_evidence)
    ev["ev-a1"]["valid_to"] = "2026-06-01T00:00:00Z"
    kept, dropped = apply_filters(
        [cand("ev-a1"), cand("ev-a2")], ev,
        FilterConfig(as_of="2026-09-01T00:00:00Z"))
    assert [c.evidence_id for c in kept] == ["ev-a2"]
    assert any("expired" in r for r in dropped[0]["reasons"])


def test_unresolved_candidate_dropped_with_reason(sample_evidence):
    kept, dropped = apply_filters(
        [cand("ev-ghost")], by_id(sample_evidence), FilterConfig())
    assert not kept
    assert any("no_evidence_record" in r for r in dropped[0]["reasons"])


def test_ranks_reassigned_after_filter(sample_evidence):
    kept, _ = apply_filters(
        [cand("ev-a1", 1), cand("ev-b1", 2), cand("ev-a2", 3)],
        by_id(sample_evidence), FilterConfig(workspace_id="default"))
    assert [c.rank for c in kept] == [1, 2]
