"""Reciprocal Rank Fusion (RRF) of multiple ranked candidate lists.

Each input list keeps its original rank/score inside every fused candidate
(``source_ranks``), so downstream gates and traces can always explain which
recall path produced a hit and at what position.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .types import Candidate

DEFAULT_RRF_K = 60


def dense_priority_merge(lists: Dict[str, List[Candidate]],
                         primary: str = "dense") -> List[Candidate]:
    """Legacy merge: primary list order preserved, then novel hits from the
    other lists appended in their own order. No score fusion."""
    merged: Dict[str, Candidate] = {}
    order: List[str] = []
    sources = [primary] + [s for s in lists if s != primary]
    for src in sources:
        for cand in lists.get(src, []):
            if cand.evidence_id not in merged:
                merged[cand.evidence_id] = cand
                order.append(cand.evidence_id)
            else:
                merged[cand.evidence_id].source_ranks.update(cand.source_ranks)
    out = []
    for rank, eid in enumerate(order, start=1):
        cand = merged[eid]
        cand.rank = rank
        cand.source = "merged"
        out.append(cand)
    return out


def interleave_merge(lists: Dict[str, List[Candidate]],
                     source_order: Optional[List[str]] = None) -> List[Candidate]:
    """Round-robin merge without score fusion: alternately take the next
    unseen candidate from each source (deterministic source order)."""
    sources = source_order or sorted(lists)
    queues = {s: list(lists.get(s, [])) for s in sources}
    seen: set = set()
    out: List[Candidate] = []
    remaining = sum(len(q) for q in queues.values())
    while remaining:
        for src in sources:
            while queues[src] and queues[src][0].evidence_id in seen:
                queues[src].pop(0)
                remaining -= 1
            if queues[src]:
                cand = queues[src].pop(0)
                remaining -= 1
                seen.add(cand.evidence_id)
                cand.source = "merged"
                out.append(cand)
    for rank, cand in enumerate(out, start=1):
        cand.rank = rank
    return out


def rrf_fuse(lists: Dict[str, List[Candidate]],
             k: int = DEFAULT_RRF_K) -> List[Candidate]:
    """Fuse ranked lists with RRF: score = sum(1 / (k + rank_in_source)).

    Deterministic: ties break on (better best-source-rank, evidence_id).
    """
    fused: Dict[str, Candidate] = {}
    for src, cands in lists.items():
        for cand in cands:
            entry = fused.get(cand.evidence_id)
            if entry is None:
                entry = Candidate(
                    evidence_id=cand.evidence_id,
                    score=0.0,
                    rank=0,
                    source="fused",
                    content=cand.content,
                    page=cand.page,
                    document_id=cand.document_id,
                    chunk_id=cand.chunk_id,
                    authority_level=cand.authority_level,
                    modality=cand.modality,
                )
                fused[cand.evidence_id] = entry
            entry.score += 1.0 / (k + cand.rank)
            entry.source_ranks.update(cand.source_ranks)
            if not entry.content and cand.content:
                entry.content = cand.content
            if entry.page is None:
                entry.page = cand.page
            if entry.authority_level is None:
                entry.authority_level = cand.authority_level
            if entry.modality == "text" and cand.modality != "text":
                entry.modality = cand.modality

    def sort_key(c: Candidate):
        best_src_rank = min((r["rank"] for r in c.source_ranks.values()),
                            default=10 ** 9)
        return (-c.score, best_src_rank, c.evidence_id)

    out = sorted(fused.values(), key=sort_key)
    for rank, cand in enumerate(out, start=1):
        cand.rank = rank
        cand.raw_score = cand.score
    return out
