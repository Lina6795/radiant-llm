"""Dense recall: thin wrapper over the existing local Chroma vector store.

Wraps ``langchain_chroma.Chroma.similarity_search_with_score`` and maps each
hit back to its ``evidence_id`` via the chunk_id stored in Chroma metadata
(with a document_id+page fallback). The original Chroma ranking is preserved
unchanged; only the identifier mapping is added.

Chroma returns L2 *distances* (lower is better). ``Candidate.score`` follows
the package-wide higher-is-better convention as a similarity in ``(0, 1]``
(``1 / (1 + distance)``); the raw distance is kept in ``raw_score`` for
explainability. Ranking is unaffected (monotonic transform).
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .types import Candidate

ENV_VECTOR_STORE = "RADIANT_VECTOR_STORE"


def default_persist_directory() -> Optional[str]:
    """Resolve the vector store directory from the environment only."""
    return os.getenv(ENV_VECTOR_STORE)


def build_chunk_to_evidence_map(
    items: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], Dict[Tuple[str, int], str]]:
    """chunk_id -> evidence_id, plus (document_id, page) -> evidence_id."""
    by_chunk: Dict[str, str] = {}
    by_page: Dict[Tuple[str, int], str] = {}
    for it in items:
        span = it.get("source_span") or {}
        chunk_id = span.get("chunk_id")
        if chunk_id:
            by_chunk[chunk_id] = it["evidence_id"]
        doc_id, page = it.get("document_id"), it.get("page")
        if doc_id is not None and page is not None:
            by_page.setdefault((doc_id, int(page)), it["evidence_id"])
    return by_chunk, by_page


class DenseRetriever:
    """Lazy-loading dense retriever; heavy imports happen on first use."""

    def __init__(
        self,
        persist_directory: Optional[str] = None,
        embedding_function: Optional[Callable] = None,
        evidence_items: Optional[List[Dict[str, Any]]] = None,
        k: int = 20,
    ) -> None:
        self.persist_directory = persist_directory or default_persist_directory()
        self._embedding_function = embedding_function
        self._vs = None
        self.k = k
        self._by_chunk: Dict[str, str] = {}
        self._by_page: Dict[Tuple[str, int], str] = {}
        self._by_eid: Dict[str, Dict[str, Any]] = {}
        if evidence_items is not None:
            self.set_evidence_items(evidence_items)

    def set_evidence_items(self, items: List[Dict[str, Any]]) -> None:
        self._by_chunk, self._by_page = build_chunk_to_evidence_map(items)
        self._by_eid = {it["evidence_id"]: it for it in items}

    def _ensure_store(self) -> None:
        if self._vs is not None:
            return
        if not self.persist_directory:
            raise RuntimeError(
                f"Dense retriever needs a persist directory; pass one or set {ENV_VECTOR_STORE}"
            )
        from langchain_chroma import Chroma

        if self._embedding_function is None:
            from utils.local_embeddings import build_local_embeddings

            self._embedding_function = build_local_embeddings()
        # NOTE: do not pass chromadb client_settings here — langchain_chroma
        # ignores persist_directory when client_settings is given (it builds
        # an ephemeral in-memory client), silently yielding an empty store.
        self._vs = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self._embedding_function,
        )

    def map_to_evidence_id(self, metadata: Dict[str, Any]) -> Optional[str]:
        """chroma metadata -> evidence_id via chunk_id, then doc+page."""
        chunk_id = metadata.get("chunk_id")
        if chunk_id and chunk_id in self._by_chunk:
            return self._by_chunk[chunk_id]
        doc_id, page = metadata.get("document_id"), metadata.get("page")
        if doc_id is not None and page is not None:
            return self._by_page.get((doc_id, int(page)))
        return None

    def query(self, question: str, k: Optional[int] = None) -> List[Candidate]:
        self._ensure_store()
        k = k or self.k
        hits = self._vs.similarity_search_with_score(question, k=k)
        out: List[Candidate] = []
        for rank, (doc, distance) in enumerate(hits, start=1):
            eid = self.map_to_evidence_id(doc.metadata or {})
            if eid is None:
                # Unmappable hit: keep it visible (never silently dropped).
                eid = f"unmapped:{doc.metadata.get('chunk_id', rank)}"
            ev = self._by_eid.get(eid, {})
            span = ev.get("source_span") or {}
            # Higher-is-better similarity in (0, 1]; raw distance preserved.
            sim = 1.0 / (1.0 + max(0.0, float(distance)))
            out.append(Candidate(
                evidence_id=eid,
                score=sim,
                rank=rank,
                source="dense",
                raw_score=float(distance),
                content=ev.get("content", doc.page_content or ""),
                page=ev.get("page", doc.metadata.get("page")),
                document_id=ev.get("document_id", doc.metadata.get("document_id")),
                chunk_id=span.get("chunk_id", doc.metadata.get("chunk_id")),
                authority_level=ev.get("authority_level"),
                modality=ev.get("modality", "text"),
                source_ranks={"dense": {"rank": rank, "score": sim}},
            ))
        return out
