"""Grader unit tests for the four verdicts."""

from retrieval.grader import (ABSTAIN, CONFLICT, ENOUGH, RETRIEVE_MORE,
                              GraderConfig, grade)
from retrieval.types import Candidate


def cand(eid, score, page=1, doc="docA", content="shared content about attention"):
    return Candidate(evidence_id=eid, score=score, rank=1, source="fused",
                     page=page, document_id=doc, content=content)


def test_enough():
    cands = [cand("a", 5.0, page=1), cand("b", 4.0, page=2)]
    out = grade(cands, GraderConfig(min_evidence=2, min_top_score=1.0))
    assert out["verdict"] == ENOUGH
    assert out["kept_count"] == 2


def test_abstain_when_empty():
    out = grade([], GraderConfig())
    assert out["verdict"] == ABSTAIN


def test_abstain_when_all_filtered():
    c = cand("a", 5.0)
    c.kept = False
    out = grade([c], GraderConfig())
    assert out["verdict"] == ABSTAIN


def test_retrieve_more_low_count():
    out = grade([cand("a", 5.0)], GraderConfig(min_evidence=3))
    assert out["verdict"] == RETRIEVE_MORE
    assert any("only 1 candidates" in r for r in out["reasons"])


def test_retrieve_more_low_score():
    cands = [cand("a", 0.5, page=1), cand("b", 0.4, page=2)]
    out = grade(cands, GraderConfig(min_evidence=2, min_top_score=1.0))
    assert out["verdict"] == RETRIEVE_MORE
    assert any("top score" in r for r in out["reasons"])


def test_conflict_same_page_divergent_content():
    a = cand("a", 5.0, page=3,
             content="alpha beta gamma delta epsilon zeta eta theta")
    b = cand("b", 4.9, page=3,
             content="one two three four five six seven eight nine ten")
    out = grade([a, b], GraderConfig(min_evidence=2, min_top_score=1.0))
    assert out["verdict"] == CONFLICT
    assert out["conflicts"][0]["page"] == 3


def test_no_conflict_when_same_page_overlapping():
    a = cand("a", 5.0, page=3, content="attention mechanisms in transformers")
    b = cand("b", 4.9, page=3, content="attention mechanisms in transformers rock")
    out = grade([a, b], GraderConfig(min_evidence=2, min_top_score=1.0))
    assert out["verdict"] == ENOUGH
    assert not out["conflicts"]


def test_no_conflict_across_pages():
    a = cand("a", 5.0, page=3, content="alpha beta gamma delta")
    b = cand("b", 4.9, page=4, content="one two three four")
    out = grade([a, b], GraderConfig(min_evidence=2, min_top_score=1.0))
    assert out["verdict"] == ENOUGH
