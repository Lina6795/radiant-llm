"""BM25 index unit tests on synthetic evidence."""

from retrieval.bm25 import BM25Index


def test_build_excludes_degraded(sample_evidence):
    idx = BM25Index().build(sample_evidence)
    assert idx.size == len(sample_evidence) - 1  # ev-d1 is degraded


def test_query_returns_ranked_candidates(sample_evidence):
    idx = BM25Index().build(sample_evidence)
    hits = idx.query("attention mechanism", top_k=5)
    assert hits, "expected hits"
    assert [c.rank for c in hits] == list(range(1, len(hits) + 1))
    assert all(c.source == "bm25" for c in hits)
    # attention-heavy chunks outrank the unrelated fox/dog chunk
    ids = [c.evidence_id for c in hits]
    assert "ev-a5" not in ids[:2]
    assert ids[0] in {"ev-a1", "ev-a2"}
    # scores are non-increasing along the ranking
    scores = [c.score for c in hits]
    assert scores == sorted(scores, reverse=True)


def test_query_before_build_raises():
    import pytest

    idx = BM25Index()
    with pytest.raises(RuntimeError):
        idx.query("anything")


def test_rebuild_picks_up_new_evidence(sample_evidence):
    idx = BM25Index().build(sample_evidence[:2])
    assert idx.size == 2
    idx.build(sample_evidence)
    assert idx.size == len(sample_evidence) - 1


def test_candidate_carries_provenance(sample_evidence):
    idx = BM25Index().build(sample_evidence)
    hit = idx.query("regularization dropout", top_k=1)[0]
    assert hit.evidence_id == "ev-a4"
    assert hit.page == 3
    assert hit.chunk_id
    assert hit.source_ranks["bm25"]["rank"] == 1
