"""S3-5: relevance + sufficiency gates -- widen once (retrieve_more), then
abstain honestly when nothing relevant exists; conflicts surface as a verdict,
never silently answered.
"""

from __future__ import annotations

import pytest

from app.retrieval.types import Candidate

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)


class EmptyDense:
    def __init__(self, items):
        pass

    def query(self, question, k=None):
        return []


class DivergentDense:
    def __init__(self, items):
        self._items = items

    def query(self, question, k=None):
        out = []
        for rank, it in enumerate(self._items[:2], start=1):
            out.append(Candidate(
                evidence_id=it["evidence_id"], score=0.9 - rank * 0.01, rank=rank,
                source="dense", content=it.get("content", ""),
                page=it.get("page"), document_id=it.get("document_id"),
            ))
        return out


def _run(kb_env, query, dense_factory):
    from app.evidence.hybrid_search import hybrid_search
    from app.evidence.store import get_evidence_store

    store = get_evidence_store(kb_env["db_path"])
    try:
        return hybrid_search(query, 3, "default", store, dense_factory=dense_factory)
    finally:
        store.close()


def test_abstain_when_nothing_matches(kb_env) -> None:
    # query has no lexical overlap with the corpus; dense returns nothing.
    # Grader verdict abstain = nothing at all, so no widen is attempted
    # (widen only fires on the retrieve_more verdict = some but insufficient).
    result = _run(kb_env, "zzzqqq xylophone quokka", lambda items: EmptyDense(items))
    assert result["sufficiency"] == "abstain"
    assert result["hits"] == []
    assert result["retrieve_more"] is False
    assert any("no candidates survived" in r or "only 0" in r for r in result["sufficiency_reasons"])


def test_enough_for_normal_query(kb_env) -> None:
    result = _run(kb_env, "What does the Transformer architecture consist of?",
                  lambda items: EmptyDense(items))
    assert result["sufficiency"] == "enough"
    assert result["hits"]
    assert result["retrieve_more"] is False


def test_same_page_conflict_surfaces_as_verdict(kb_env) -> None:
    # dense lane reports two candidates on the SAME page of the same document
    # with near-zero content overlap; query has no lexical match so the fused
    # candidates carry the dense lane's crafted metadata.
    class ConflictDense:
        def __init__(self, items):
            self._items = items

        def query(self, question, k=None):
            out = []
            contents = ["alpha beta gamma xylophone", "delta epsilon zeta quokka"]
            for rank, it in enumerate(self._items[:2], start=1):
                out.append(Candidate(
                    evidence_id=it["evidence_id"], score=1.0 - rank * 0.001, rank=rank,
                    source="dense", content=contents[rank - 1],
                    page=1, document_id=it.get("document_id"),
                ))
            return out

    result = _run(kb_env, "xylophone quokka", lambda items: ConflictDense(items))
    assert result["sufficiency"] == "conflict"
    # conflicts still return hits, but the verdict+reasons make it explicit
    assert result["hits"]
    assert any("divergent" in r or "conflict" in r for r in result["sufficiency_reasons"])
