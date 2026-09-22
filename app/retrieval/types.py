"""Shared retrieval data types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Deterministic lowercase alphanumeric tokenizer shared by BM25,
    the proxy reranker and the conflict heuristic."""
    return _TOKEN_RE.findall((text or "").lower())


@dataclass
class Candidate:
    """One retrieval candidate flowing through the pipeline.

    ``score`` is always higher-is-better, regardless of the source's raw
    metric; the raw value is kept under ``raw_score`` for explainability.
    """

    evidence_id: str
    score: float
    rank: int
    source: str  # bm25 | dense | fused | reranked
    raw_score: Optional[float] = None
    content: str = ""
    page: Optional[int] = None
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    authority_level: Optional[str] = None
    # Per-source provenance filled in by fusion: {source: {"rank","score"}}.
    source_ranks: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Filter / gate bookkeeping: reason strings, in application order.
    filter_reasons: list[str] = field(default_factory=list)
    kept: bool = True
    gate_notes: list[str] = field(default_factory=list)

    def to_trace(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "score": round(self.score, 6),
            "rank": self.rank,
            "source": self.source,
            "raw_score": self.raw_score,
            "page": self.page,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "authority_level": self.authority_level,
            "source_ranks": self.source_ranks,
            "kept": self.kept,
            "filter_reasons": list(self.filter_reasons),
            "gate_notes": list(self.gate_notes),
        }
