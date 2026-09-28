"""S4-3: evidence preparation before selection -- content dedup, per-source
quota, and neighbor expansion. Everything here is deterministic and returns
dropped/notes lists so the package trace can explain every mutation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from .selector import EvidenceItem
from .tokenizer import TokenCounter, default_counter

_CHUNK_RE = re.compile(r"^(?P<doc>.+):p(?P<page>\d+):c(?P<idx>\d+)$")


def dedup_items(items: List[EvidenceItem]) -> Tuple[List[EvidenceItem], List[Dict[str, Any]]]:
    """Drop later duplicates by chunk_id, then by content hash. First wins."""
    kept: List[EvidenceItem] = []
    dropped: List[Dict[str, Any]] = []
    seen_chunks: set = set()
    seen_content: set = set()
    for it in items:
        if it.chunk_id and it.chunk_id in seen_chunks:
            dropped.append({"evidence_id": it.evidence_id, "reason": "duplicate_chunk_id",
                            "chunk_id": it.chunk_id})
            continue
        digest = hashlib.sha1(it.content.encode("utf-8")).hexdigest()
        if digest in seen_content:
            dropped.append({"evidence_id": it.evidence_id, "reason": "duplicate_content"})
            continue
        if it.chunk_id:
            seen_chunks.add(it.chunk_id)
        seen_content.add(digest)
        kept.append(it)
    return kept, dropped


def apply_source_quota(
    items: List[EvidenceItem],
    budget_tokens: int,
    *,
    max_share: float = 0.6,
    counter: Optional[TokenCounter] = None,
) -> Tuple[List[EvidenceItem], List[Dict[str, Any]]]:
    """No single document may consume more than ``max_share`` of the evidence
    budget (except pinned items, which are anchor-protected by contract).
    Input order preserved; overflow items are dropped with a reason."""
    counter = counter or default_counter()
    cap = int(budget_tokens * max_share)
    kept: List[EvidenceItem] = []
    dropped: List[Dict[str, Any]] = []
    per_doc: Dict[str, int] = {}
    for it in items:
        tokens = it.tokens(counter)
        doc = it.document_id or "<none>"
        if it.pinned:
            per_doc[doc] = per_doc.get(doc, 0) + tokens
            kept.append(it)
            continue
        if per_doc.get(doc, 0) + tokens > cap:
            dropped.append({
                "evidence_id": it.evidence_id,
                "reason": f"source_quota: document {doc} would exceed "
                          f"{max_share:.0%} of the evidence budget",
                "document_id": doc,
                "tokens": tokens,
            })
            continue
        per_doc[doc] = per_doc.get(doc, 0) + tokens
        kept.append(it)
    return kept, dropped


def _neighbor_chunk_id(chunk_id: str, delta: int) -> Optional[str]:
    match = _CHUNK_RE.match(chunk_id or "")
    if not match:
        return None
    idx = int(match.group("idx")) + delta
    if idx < 0:
        return None
    return f"{match.group('doc')}:p{match.group('page')}:c{idx}"


def expand_neighbors(
    items: List[EvidenceItem],
    index_by_chunk: Dict[str, EvidenceItem],
    *,
    max_per_item: int = 1,
) -> Tuple[List[EvidenceItem], List[str]]:
    """For each input item, admit up to ``max_per_item`` adjacent chunks
    (c+1 first, then c-1) from the same document, tagged ``neighbor_of``.
    Neighbors already present (by evidence_id) are skipped."""
    present = {i.evidence_id for i in items}
    out = list(items)
    notes: List[str] = []
    for it in items:
        if not it.chunk_id:
            # S7: visual items have no chunk_id; their neighbor is a TEXT
            # chunk on the same page of the same document (visual-to-text
            # neighbor expansion).
            if it.document_id and it.page is not None:
                neighbor = next(
                    (c for c in index_by_chunk.values()
                     if c.evidence_id not in present
                     and c.document_id == it.document_id
                     and c.page == it.page
                     and c.modality == "text"),
                    None,
                )
                if neighbor is not None:
                    clone = EvidenceItem(
                        evidence_id=neighbor.evidence_id,
                        content=neighbor.content,
                        page=neighbor.page,
                        score=neighbor.score,
                        authority_level=neighbor.authority_level,
                        valid_from=neighbor.valid_from,
                        pinned=False,
                        document_id=neighbor.document_id,
                        chunk_id=neighbor.chunk_id,
                        modality=neighbor.modality,
                        neighbor_of=it.evidence_id,
                    )
                    present.add(neighbor.evidence_id)
                    out.append(clone)
                    notes.append(f"visual_to_text_neighbor: {neighbor.evidence_id} admitted as same-page text of {it.evidence_id}")
            continue
        admitted = 0
        for delta in (1, -1):
            if admitted >= max_per_item:
                break
            nid = _neighbor_chunk_id(it.chunk_id, delta)
            if nid is None:
                continue
            neighbor = index_by_chunk.get(nid)
            if neighbor is None or neighbor.evidence_id in present:
                continue
            clone = EvidenceItem(
                evidence_id=neighbor.evidence_id,
                content=neighbor.content,
                page=neighbor.page,
                score=neighbor.score,
                authority_level=neighbor.authority_level,
                valid_from=neighbor.valid_from,
                pinned=False,
                document_id=neighbor.document_id,
                chunk_id=neighbor.chunk_id,
                modality=neighbor.modality,
                neighbor_of=it.evidence_id,
            )
            present.add(neighbor.evidence_id)
            out.append(clone)
            admitted += 1
            notes.append(f"neighbor_expansion: {neighbor.evidence_id} admitted as neighbor of {it.evidence_id}")
    return out, notes
