"""Proxy reranker + dense mapping unit tests."""

import pytest

from retrieval.dense import DenseRetriever, build_chunk_to_evidence_map
from retrieval.rerank import (RerankConfig, TokenOverlapReranker,
                              apply_rerank, build_reranker)
from retrieval.types import Candidate


def cand(eid, score, rank, content=""):
    return Candidate(evidence_id=eid, score=score, rank=rank, source="fused",
                     content=content)


# ---------------------------------------------------------------------------
# Proxy reranker
# ---------------------------------------------------------------------------

def test_rerank_disabled_is_noop():
    cands = [cand("a", 1.0, 1, "attention"), cand("b", 0.5, 2, "dropout")]
    out = apply_rerank(cands, "attention", RerankConfig(enabled=False))
    assert [c.evidence_id for c in out] == ["a", "b"]


def test_proxy_reranker_prefers_query_overlap():
    cands = [
        cand("off", 10.0, 1, "banana bread recipe with walnuts and honey"),
        cand("on", 0.1, 2, "scaled dot product attention over queries keys values"),
    ]
    out = apply_rerank(cands, "dot product attention queries",
                       RerankConfig(enabled=True))
    assert out[0].evidence_id == "on"
    assert out[0].source_ranks["pre_rerank"]["rank"] == 2
    assert out[1].source_ranks["pre_rerank"]["rank"] == 1


def test_proxy_reranker_deterministic():
    r = TokenOverlapReranker()
    contents = ["self attention layers", "unrelated kitchen text", "attention"]
    assert r.score("attention", contents) == r.score("attention", contents)


def test_unknown_reranker_type_rejected():
    with pytest.raises(ValueError):
        build_reranker(RerankConfig(enabled=True, reranker_type="cohere"))


def test_rerank_tail_preserved_beyond_top_n():
    cands = [cand(f"c{i}", 1.0 - i * 0.01, i + 1, f"text {i}") for i in range(6)]
    out = apply_rerank(cands, "zzzz-nomatch", RerankConfig(enabled=True, top_n=3))
    # all proxy scores 0 -> head keeps prior order; tail untouched
    assert [c.evidence_id for c in out] == [f"c{i}" for i in range(6)]


# ---------------------------------------------------------------------------
# chunk_id -> evidence_id mapping (dense neighbor mapping)
# ---------------------------------------------------------------------------

def test_chunk_to_evidence_map(sample_evidence):
    by_chunk, by_page = build_chunk_to_evidence_map(sample_evidence)
    assert by_chunk["docA:p1:c1"] == "ev-a1"
    assert by_page[("docB", 6)] == "ev-b1"


def test_dense_map_metadata_prefers_chunk_id(sample_evidence):
    retriever = DenseRetriever(persist_directory=None,
                               evidence_items=sample_evidence)
    assert retriever.map_to_evidence_id(
        {"chunk_id": "docA:p1:c1", "page": 99, "document_id": "docA"}) == "ev-a1"


def test_dense_map_metadata_page_fallback(sample_evidence):
    retriever = DenseRetriever(persist_directory=None,
                               evidence_items=sample_evidence)
    assert retriever.map_to_evidence_id(
        {"document_id": "docA", "page": 3}) == "ev-a4"
    assert retriever.map_to_evidence_id({"page": 999}) is None


def test_dense_query_without_directory_raises(monkeypatch):
    monkeypatch.delenv("RADIANT_VECTOR_STORE", raising=False)
    retriever = DenseRetriever(persist_directory=None)
    with pytest.raises(RuntimeError):
        retriever.query("anything")
