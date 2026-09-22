"""Gate unit tests: relevance pruning + anchor/authority protection."""

from retrieval.gate import (AnchorGateConfig, RelevanceGateConfig,
                            apply_anchor_gate, apply_relevance_gate)
from retrieval.types import Candidate


def cand(eid, score, rank, page=None, doc="docA", authority="primary",
         chunk=None):
    return Candidate(evidence_id=eid, score=score, rank=rank, source="fused",
                     page=page, document_id=doc, authority_level=authority,
                     chunk_id=chunk)


def test_relevance_gate_absolute_floor():
    cands = [cand("a", 10.0, 1), cand("b", 0.4, 2), cand("c", 0.1, 3)]
    kept, dropped = apply_relevance_gate(
        cands, RelevanceGateConfig(enabled=True, min_score=0.5))
    assert [c.evidence_id for c in kept] == ["a"]
    assert {d["evidence_id"] for d in dropped} == {"b", "c"}
    assert all("below_min_score" in d["reasons"][0] for d in dropped)


def test_relevance_gate_relative_ratio():
    cands = [cand("a", 10.0, 1), cand("b", 4.0, 2), cand("c", 0.3, 3)]
    kept, dropped = apply_relevance_gate(
        cands, RelevanceGateConfig(enabled=True, min_score_ratio=0.2))
    assert [c.evidence_id for c in kept] == ["a", "b"]
    assert dropped[0]["evidence_id"] == "c"


def test_relevance_gate_min_keep_restores_one():
    cands = [cand("a", 0.0, 1)]
    kept, _ = apply_relevance_gate(
        cands, RelevanceGateConfig(enabled=True, min_score=1.0, min_keep=1))
    assert len(kept) == 1
    assert "restored_by_min_keep" in kept[0].gate_notes


def test_relevance_gate_drop_cut():
    cands = [cand("a", 10.0, 1), cand("b", 9.0, 2), cand("c", 1.0, 3),
             cand("d", 0.9, 4)]
    kept, dropped = apply_relevance_gate(
        cands, RelevanceGateConfig(enabled=True, max_drop_ratio=0.5))
    assert [c.evidence_id for c in kept] == ["a", "b"]
    assert {d["evidence_id"] for d in dropped} == {"c", "d"}


def test_anchor_gate_promotes_anchor_match_into_window():
    # Broad high scorers push the gold-page evidence to rank 6.
    cands = [cand(f"broad{i}", 10.0 - i, i + 1, page=10 + i) for i in range(5)]
    cands.append(cand("gold", 1.0, 6, page=3, chunk="docA:p3:c1"))
    gated, notes = apply_anchor_gate(
        cands, {"page": 3, "document_id": "docA"},
        AnchorGateConfig(enabled=True, protect_top_k=5,
                         apply_authority_weight=False))
    ids = [c.evidence_id for c in gated]
    assert ids.index("gold") < 5, "anchor evidence must be pinned in top-5"
    assert notes and notes[0]["evidence_id"] == "gold"
    assert any("anchor_protected" in n
               for n in gated[ids.index("gold")].gate_notes)


def test_anchor_gate_multiple_matches_all_fit_window():
    cands = [cand(f"broad{i}", 10.0 - i, i + 1, page=20 + i) for i in range(4)]
    cands.append(cand("gold1", 2.0, 5, page=3))
    cands.append(cand("gold2", 1.0, 6, page=3))
    gated, notes = apply_anchor_gate(
        cands, {"page": 3, "document_id": "docA"},
        AnchorGateConfig(enabled=True, protect_top_k=4,
                         apply_authority_weight=False))
    window = [c.evidence_id for c in gated[:4]]
    assert "gold1" in window and "gold2" in window
    assert len(notes) == 2


def test_anchor_gate_noop_without_anchor():
    cands = [cand("a", 2.0, 1), cand("b", 1.0, 2)]
    gated, notes = apply_anchor_gate(
        cands, None,
        AnchorGateConfig(enabled=True, apply_authority_weight=False))
    assert [c.evidence_id for c in gated] == ["a", "b"]
    assert notes == []


def test_authority_weighting_reorders():
    cands = [cand("sec", 5.0, 1, authority="secondary"),
             cand("pri", 4.5, 2, authority="primary")]
    weights = {"primary": 1.0, "secondary": 0.5}
    gated, _ = apply_anchor_gate(
        cands, None,
        AnchorGateConfig(enabled=True, authority_weights=weights,
                         apply_authority_weight=True))
    # 4.5*1.0 = 4.5 beats 5.0*0.5 = 2.5
    assert gated[0].evidence_id == "pri"
    assert any("authority_weight" in n for n in gated[1].gate_notes)


def test_anchor_gate_does_not_fabricate_missing_anchor():
    cands = [cand("x", 5.0, 1, page=9)]
    gated, notes = apply_anchor_gate(
        cands, {"page": 3, "document_id": "docA"},
        AnchorGateConfig(enabled=True, apply_authority_weight=False))
    assert [c.evidence_id for c in gated] == ["x"]
    assert notes == []
