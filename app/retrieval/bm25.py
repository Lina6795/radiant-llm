"""BM25 lexical recall over authoritative text evidence.

The index is built from the EvidenceStore's non-degraded ``text`` evidence
(degraded rows are never authoritative-answer candidates, per the store
contract). It can be rebuilt at any time via :meth:`BM25Index.rebuild`.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from rank_bm25 import BM25Okapi

from .types import Candidate, tokenize


def load_text_evidence(store, modality: str = "text",
                       include_degraded: bool = False,
                       current_only: bool = True,
                       workspace_id: Optional[str] = None,
                       limit: int = 1000) -> List[Dict[str, Any]]:
    """Pull evidence payloads from an EvidenceStore for indexing.

    ``current_only`` defaults to True (S3-2): a retrieval index must contain
    only currently valid rows -- superseded versions never enter BM25/Dense.
    Pass ``current_only=False`` only for offline forensics over the full
    version history. ``workspace_id`` scopes the index to one workspace.
    """
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = store.query_evidence(
            modality=modality, include_degraded=include_degraded,
            current_only=current_only, workspace_id=workspace_id,
            limit=limit, offset=offset,
        )
        items.extend(page["items"])
        if len(items) >= page["total"] or not page["items"]:
            break
        offset += len(page["items"])
    return items


class BM25Index:
    """Reusable BM25 index over evidence payloads."""

    def __init__(self) -> None:
        self._bm25: Optional[BM25Okapi] = None
        self._items: List[Dict[str, Any]] = []
        self.built_at: Optional[float] = None

    def build(self, items: List[Dict[str, Any]]) -> "BM25Index":
        # Figure-level visual records are degraded ONLY for the missing bbox
        # (honest page/figure-level evidence) and stay indexable; every other
        # degraded record is excluded (S7).
        self._items = [
            it for it in items
            if not it.get("degraded")
            or (it.get("modality") == "visual"
                and it.get("degraded_reason") == "missing_region_bbox")
        ]
        corpus = [tokenize(it.get("content", "")) for it in self._items]
        self._bm25 = BM25Okapi(corpus)
        self.built_at = time.time()
        return self

    @classmethod
    def from_store(cls, store, modality: str = "text") -> "BM25Index":
        return cls().build(load_text_evidence(store, modality=modality))

    def rebuild(self, store, modality: str = "text") -> "BM25Index":
        return self.build(load_text_evidence(store, modality=modality))

    @property
    def size(self) -> int:
        return len(self._items)

    def query(self, question: str, top_k: int = 20) -> List[Candidate]:
        """Return ``(evidence_id, score, rank)``-style candidates, best first."""
        if self._bm25 is None:
            raise RuntimeError("BM25Index.query before build()")
        scores = self._bm25.get_scores(tokenize(question))
        order = sorted(range(len(scores)),
                       key=lambda i: (-scores[i], self._items[i]["evidence_id"]))
        out: List[Candidate] = []
        for rank, i in enumerate(order[:top_k], start=1):
            it = self._items[i]
            span = it.get("source_span") or {}
            out.append(Candidate(
                evidence_id=it["evidence_id"],
                score=float(scores[i]),
                rank=rank,
                source="bm25",
                raw_score=float(scores[i]),
                content=it.get("content", ""),
                page=it.get("page"),
                document_id=it.get("document_id"),
                chunk_id=span.get("chunk_id"),
                authority_level=it.get("authority_level"),
                modality=it.get("modality", "text"),
                source_ranks={"bm25": {"rank": rank, "score": float(scores[i])}},
            ))
        return out
