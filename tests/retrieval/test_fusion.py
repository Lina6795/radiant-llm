"""RRF fusion and dense-priority merge unit tests."""

from retrieval.fusion import dense_priority_merge, rrf_fuse
from retrieval.types import Candidate


def make(eid, rank, source, score=1.0):
    return Candidate(evidence_id=eid, score=score, rank=rank, source=source,
                     source_ranks={source: {"rank": rank, "score": score}})


def test_rrf_scores_and_ordering():
    dense = [make("a", 1, "dense"), make("b", 2, "dense"), make("c", 3, "dense")]
    bm25 = [make("b", 1, "bm25"), make("d", 2, "bm25"), make("a", 3, "bm25")]
    fused = rrf_fuse({"dense": dense, "bm25": bm25}, k=60)
    by_id = {c.evidence_id: c for c in fused}
    # b appears in both lists with high ranks -> must win
    assert fused[0].evidence_id == "b"
    # exact RRF math
    assert abs(by_id["b"].score - (1 / 61 + 1 / 62)) < 1e-9
    assert abs(by_id["c"].score - 1 / 63) < 1e-9
    assert [c.rank for c in fused] == [1, 2, 3, 4]


def test_rrf_preserves_per_source_ranks():
    dense = [make("a", 1, "dense")]
    bm25 = [make("a", 2, "bm25")]
    fused = rrf_fuse({"dense": dense, "bm25": bm25})
    assert fused[0].source_ranks["dense"]["rank"] == 1
    assert fused[0].source_ranks["bm25"]["rank"] == 2


def test_rrf_deterministic_tie_break():
    l1 = [make("x", 1, "dense"), make("y", 2, "dense")]
    l2 = [make("y", 1, "bm25"), make("x", 2, "bm25")]
    fused1 = rrf_fuse({"dense": l1, "bm25": l2})
    fused2 = rrf_fuse({"bm25": l2, "dense": l1})
    assert [c.evidence_id for c in fused1] == [c.evidence_id for c in fused2]


def test_dense_priority_merge_keeps_primary_order():
    dense = [make("a", 1, "dense"), make("b", 2, "dense")]
    bm25 = [make("c", 1, "bm25"), make("a", 2, "bm25")]
    merged = dense_priority_merge({"dense": dense, "bm25": bm25})
    assert [c.evidence_id for c in merged] == ["a", "b", "c"]
    by_id = {c.evidence_id: c for c in merged}
    assert by_id["a"].source_ranks["bm25"]["rank"] == 2


def test_interleave_merge_round_robins():
    from retrieval.fusion import interleave_merge

    dense = [make("d1", 1, "dense"), make("d2", 2, "dense"), make("x", 3, "dense")]
    bm25 = [make("b1", 1, "bm25"), make("x", 2, "bm25"), make("b2", 3, "bm25")]
    merged = interleave_merge({"dense": dense, "bm25": bm25},
                              source_order=["dense", "bm25"])
    assert [c.evidence_id for c in merged] == ["d1", "b1", "d2", "x", "b2"]
    assert [c.rank for c in merged] == [1, 2, 3, 4, 5]
