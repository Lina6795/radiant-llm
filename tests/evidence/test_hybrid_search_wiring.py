"""S3-3: hybrid retrieval wiring (BM25 + Dense + RRF) in the production
evidence.search handler -- lane trace, fail-closed strict mode, and honest
auto-mode fallback. Dense lane is injected (FakeDense) so these tests stay
offline; the real BGE+Chroma path is covered by the S3 smoke artifacts.
"""

from __future__ import annotations

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry
from app.retrieval.types import Candidate

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)


class FakeDense:
    def __init__(self, items):
        ids = [it["evidence_id"] for it in items]
        self._ids = ids

    def query(self, question, k=None):
        # deterministic: reversed order, distinct from BM25
        out = []
        for rank, eid in enumerate(reversed(self._ids), start=1):
            out.append(Candidate(evidence_id=eid, score=1.0 / rank, rank=rank, source="dense"))
        return out[: k or 20]


def _invoke(kb_env, arguments=None, mode=None, monkeypatch=None):
    if monkeypatch is not None:
        if mode is None:
            monkeypatch.delenv("RADIANT_EVIDENCE_SEARCH_MODE", raising=False)
        else:
            monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_MODE", mode)
        monkeypatch.delenv("RADIANT_VECTOR_STORE", raising=False)
    registry = build_default_registry()
    return registry.invoke(
        "evidence.search",
        arguments or {"query": "What does the Transformer architecture consist of?", "top_k": 3},
        run_id="s3-3", workspace="default",
    )


def test_hybrid_returns_lane_trace_and_rrf_fusion(kb_env, monkeypatch) -> None:
    from app.evidence.hybrid_search import hybrid_search

    store = __import__("app.evidence.store", fromlist=["get_evidence_store"]).get_evidence_store(
        kb_env["db_path"]
    )
    try:
        result = hybrid_search("What does the Transformer architecture consist of?", 3, "default",
                               store, dense_factory=lambda items: FakeDense(items))
    finally:
        store.close()
    assert result["hits"]
    lanes = result["lanes"]
    # zero-score BM25 candidates are filtered (S3-5): only lexically-matching
    # chunks appear in the bm25 lane
    assert len(lanes["bm25"]) >= 2 and len(lanes["dense"]) >= 3
    # fused output is a merged ranking: every fused candidate carries an rrf score
    fused = lanes["fused"]
    assert fused[0]["rank"] == 1
    assert all("score" in c and "evidence_id" in c for c in fused)
    # RRF(k=60) sanity: dense rank-1 (reversed last id) and bm25 rank-1 both
    # contribute 1/61; whoever ranks best across both lanes wins.
    bm25_top = lanes["bm25"][0]["evidence_id"]
    dense_top = lanes["dense"][0]["evidence_id"]
    assert bm25_top != dense_top  # lanes genuinely differ for this fixture
    assert fused[0]["score"] >= 1.0 / 61


def test_hybrid_strict_mode_fails_closed_without_vector_store(kb_env, monkeypatch) -> None:
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_MODE", "hybrid")
    monkeypatch.delenv("RADIANT_VECTOR_STORE", raising=False)
    registry = build_default_registry()
    result = registry.invoke(
        "evidence.search", {"query": "transformer", "top_k": 3}, run_id="s3-3", workspace="default"
    )
    assert result.status == ToolStatus.TERMINAL_ERROR
    assert result.error.code == "evidence.vector_store_missing"
    assert result.error.retryable is False


def test_auto_mode_falls_back_to_keyword_with_reason(kb_env, monkeypatch) -> None:
    result = _invoke(kb_env, mode="auto", monkeypatch=monkeypatch)
    assert result.status == ToolStatus.SUCCESS
    assert result.output["retrieval"] == "kb_keyword+store_resolve"
    assert result.output["hybrid_fallback_reason"] == "evidence.vector_store_missing"
    assert result.output["hits"]


def test_auto_mode_uses_hybrid_when_vector_store_present(kb_env, monkeypatch, tmp_path) -> None:
    vs = tmp_path / "vs"
    vs.mkdir()
    monkeypatch.setenv("RADIANT_VECTOR_STORE", str(vs))
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_MODE", "auto")
    # dense lane injected via the module-level hook to stay offline
    from app.evidence import hybrid_search as hybrid_mod

    real_indexes = hybrid_mod._build_indexes

    def stub(store, workspace, dense_factory):
        return real_indexes(store, workspace, dense_factory or (lambda items: FakeDense(items)))

    monkeypatch.setattr(hybrid_mod, "_build_indexes", stub)
    hybrid_mod._cache.clear()
    registry = build_default_registry()
    result = registry.invoke(
        "evidence.search",
        {"query": "What does the Transformer architecture consist of?", "top_k": 3},
        run_id="s3-3", workspace="default",
    )
    assert result.status == ToolStatus.SUCCESS
    assert result.output["retrieval"] == "hybrid_bm25_dense_rrf"
    assert result.output["lanes"]["bm25"]
    assert result.output["lanes"]["dense"]
    assert result.output["hits"]


def test_keyword_mode_stays_keyword(kb_env, monkeypatch) -> None:
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_MODE", "keyword")
    monkeypatch.delenv("RADIANT_VECTOR_STORE", raising=False)
    registry = build_default_registry()
    result = registry.invoke(
        "evidence.search", {"query": "transformer", "top_k": 3}, run_id="s3-3", workspace="default"
    )
    assert result.status == ToolStatus.SUCCESS
    assert result.output["retrieval"] == "kb_keyword+store_resolve"
    assert "hybrid_fallback_reason" not in result.output
