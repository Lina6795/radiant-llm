"""S3-3: production hybrid retrieval -- BM25 + Dense fused with RRF.

Built on the existing offline retrieval stack (app/retrieval): BM25Okapi over
currently-valid evidence (S3-2), Chroma dense lane (RADIANT_VECTOR_STORE, same
metadata->evidence_id mapping), fused with rrf_k=60. Every call returns the
per-lane rankings AND the final fused ranking so the trace can explain each
candidate's provenance.

Indexes are cached per process, keyed by (workspace, evidence-id fingerprint),
so evidence mutations invalidate them without a restart.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Callable, Dict, List, Optional

from app.retrieval.bm25 import BM25Index, load_text_evidence
from app.retrieval.dense import DenseRetriever
from app.retrieval.fusion import rrf_fuse
from app.retrieval.types import Candidate

RRF_K = 60
RECALL_K = 20

_cache_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}


class HybridSearchUnavailable(Exception):
    """Dense lane cannot be built (e.g. vector store missing)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _evidence_fingerprint(items: List[Dict[str, Any]]) -> str:
    ids = sorted(it["evidence_id"] for it in items)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]


def _build_indexes(
    store, workspace: str, dense_factory: Optional[Callable[..., DenseRetriever]]
) -> Dict[str, Any]:
    # S7: text (non-degraded) + visual (figure-level, degraded only for the
    # missing-bbox reason -- honest page/figure-level evidence) both enter
    # the index; their modality travels on every candidate.
    items = load_text_evidence(store, workspace_id=workspace)
    items += load_text_evidence(store, modality="visual", include_degraded=True,
                                workspace_id=workspace)
    if not items:
        raise HybridSearchUnavailable(
            "evidence.store_empty",
            f"workspace '{workspace}' has no currently-valid evidence to index",
        )
    bm25 = BM25Index().build(items)
    if dense_factory is not None:
        dense = dense_factory(items)
    else:
        dense = DenseRetriever(evidence_items=items)
    return {
        "fingerprint": _evidence_fingerprint(items),
        "items": items,
        "by_id": {it["evidence_id"]: it for it in items},
        "bm25": bm25,
        "dense": dense,
    }


def _indexes(
    store, workspace: str, dense_factory: Optional[Callable[..., DenseRetriever]]
) -> Dict[str, Any]:
    key = f"{getattr(store, 'db_path', id(store))}::{workspace}"
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        # invalidate on any evidence mutation (cheap fingerprint over ids)
        current_fp = _evidence_fingerprint(load_text_evidence(store, workspace_id=workspace))
        if current_fp == cached["fingerprint"]:
            return cached
    built = _build_indexes(store, workspace, dense_factory)
    with _cache_lock:
        _cache[key] = built
    return built


def hybrid_search(
    query: str,
    top_k: int,
    workspace: str,
    store,
    *,
    dense_factory: Optional[Callable[..., DenseRetriever]] = None,
) -> Dict[str, Any]:
    """BM25 + Dense + RRF over currently-valid evidence of one workspace.

    Returns hits plus the full lane trace. Raises HybridSearchUnavailable
    (typed) when the dense lane or the evidence index cannot be built; the
    caller decides fallback semantics.
    """
    idx = _indexes(store, workspace, dense_factory)
    if dense_factory is None:
        import os

        vs_dir = (os.getenv("RADIANT_VECTOR_STORE") or "").strip()
        if not vs_dir or not __import__("pathlib").Path(vs_dir).is_dir():
            raise HybridSearchUnavailable(
                "evidence.vector_store_missing",
                "RADIANT_VECTOR_STORE is not set or not a directory; dense lane is unavailable",
            )
    recall_k = max(RECALL_K, top_k * 4)
    by_id = idx["by_id"]

    from app.retrieval.filters import FilterConfig, apply_filters
    from app.retrieval.gate import RelevanceGateConfig, apply_relevance_gate
    from app.retrieval.grader import GraderConfig, grade

    def _recall_fuse_gate(recall_k: int):
        lists = {
            # zero BM25 score = no lexical match at all; not a candidate
            "bm25": [c for c in idx["bm25"].query(query, top_k=recall_k) if c.score > 0],
        }
        try:
            lists["dense"] = idx["dense"].query(query, k=recall_k)
        except RuntimeError as exc:
            raise HybridSearchUnavailable("evidence.vector_store_missing", str(exc)) from exc
        fused = rrf_fuse(lists, k=RRF_K)

        # S7-3: figure/table reference boost -- a query naming "Figure 2" or
        # "Table 1" is a strong deterministic signal for the visual record
        # whose description names that exact reference; worth one RRF
        # first-place vote, and recorded in the trace.
        import re as _re

        refs = {(m.group(1).lower().rstrip("."), m.group(2))
                for m in _re.finditer(r"(?i)\b(figure|fig\.?|table)\s*(\d+)", query)}
        boosted: List[str] = []
        if refs:
            for cand in fused:
                if cand.modality != "visual":
                    continue
                content_l = (cand.content or "").lower()
                for kind, num in refs:
                    kind_full = "figure" if kind.startswith("fig") else kind
                    if f"{kind_full} {num}" in content_l or f"{kind_full} {num}:" in content_l:
                        cand.score += 1.0 / (RRF_K + 1)
                        boosted.append(f"figure_ref_boost:{cand.evidence_id}:{kind_full} {num}")
                        break
            if boosted:
                fused = sorted(
                    fused,
                    key=lambda c: (-c.score,
                                   min((r["rank"] for r in c.source_ranks.values()), default=10**9),
                                   c.evidence_id),
                )
                for rank, cand in enumerate(fused, start=1):
                    cand.rank = rank
        gate_map = dict(by_id)
        for cand in fused:
            if cand.evidence_id.startswith("unmapped:"):
                continue
            if cand.evidence_id not in gate_map:
                ev = store.get_evidence(cand.evidence_id)
                if ev is not None:
                    gate_map[cand.evidence_id] = ev
        # S7: figure-level visual records are degraded ONLY for the missing
        # bbox -- honest evidence, not unreliable content. Exempt exactly that
        # degraded reason in the gate VIEW (store rows untouched).
        gate_view = {
            eid: (dict(ev, degraded=False)
                  if ev.get("modality") == "visual"
                  and ev.get("degraded_reason") == "missing_region_bbox"
                  else ev)
            for eid, ev in gate_map.items()
        }
        kept, dropped = apply_filters(
            fused, gate_view, FilterConfig(workspace_id=workspace, exclude_degraded=True)
        )
        # S3-5: relevance gate -- scale-free ratio of the fused top score.
        kept, rel_dropped = apply_relevance_gate(
            kept, RelevanceGateConfig(enabled=True, min_score_ratio=0.5, min_keep=1)
        )
        return lists, fused, kept, dropped + rel_dropped, boosted

    lists, fused, kept, dropped, boosted = _recall_fuse_gate(recall_k)

    grader_cfg = GraderConfig(min_evidence=1, min_top_score=0.0)
    verdict = grade(kept, grader_cfg)
    retrieve_more_used = False
    if verdict["verdict"] == "retrieve_more":
        # S3-5: one bounded widen of the recall, then accept the verdict --
        # never silently pad with unrelated candidates.
        retrieve_more_used = True
        lists, fused, kept, dropped, boosted = _recall_fuse_gate(min(recall_k * 2, 80))
        verdict = grade(kept, grader_cfg)

    abstain = verdict["verdict"] == "abstain"

    hits: List[Dict[str, Any]] = []
    unresolved = 0
    for cand in ([] if abstain else kept):
        if cand.evidence_id.startswith("unmapped:"):
            unresolved += 1
            continue
        ev = by_id.get(cand.evidence_id)
        if ev is None:
            unresolved += 1
            continue
        span = ev.get("source_span") or {}
        hits.append(
            {
                "evidence_id": cand.evidence_id,
                "document_id": ev.get("document_id"),
                "chunk_id": span.get("chunk_id"),
                "page": ev.get("page"),
                "modality": ev.get("modality", "text"),
                "figure_id": ev.get("figure_id"),
                "source": ev.get("artifact_uri") or ev.get("document_id"),
                "score": round(cand.score, 6),
                "snippet": (ev.get("content") or "")[:240],
            }
        )
        if len(hits) >= top_k:
            break

    # S7-4: modality quota -- when visual candidates survived the gates, at
    # least one visual hit is guaranteed in the final top_k (text may not
    # crowd out every visual candidate).
    quota_notes: List[str] = []
    if not abstain and hits and len(hits) >= top_k:
        has_visual = any(h.get("modality") == "visual" for h in hits)
        if not has_visual:
            best_visual = next(
                (c for c in kept if not c.evidence_id.startswith("unmapped:")
                 and (by_id.get(c.evidence_id) or {}).get("modality") == "visual"),
                None,
            )
            if best_visual is not None:
                ev = by_id[best_visual.evidence_id]
                span = ev.get("source_span") or {}
                hits[-1] = {
                    "evidence_id": best_visual.evidence_id,
                    "document_id": ev.get("document_id"),
                    "chunk_id": span.get("chunk_id"),
                    "page": ev.get("page"),
                    "modality": "visual",
                    "figure_id": ev.get("figure_id"),
                    "source": ev.get("artifact_uri") or ev.get("document_id"),
                    "score": round(best_visual.score, 6),
                    "snippet": (ev.get("content") or "")[:240],
                }
                quota_notes.append(
                    f"modality_quota: visual candidate {best_visual.evidence_id} reserved in top_k")

    def _lane(candidates: List[Candidate]) -> List[Dict[str, Any]]:
        return [
            {"rank": c.rank, "evidence_id": c.evidence_id, "score": round(c.score, 6)}
            for c in candidates[: max(top_k * 2, 6)]
        ]

    return {
        "hits": hits,
        "unresolved_count": unresolved,
        "lanes": {
            "bm25": _lane(lists["bm25"]),
            "dense": _lane(lists["dense"]),
            "fused": _lane(fused),
        },
        "rrf_k": RRF_K,
        "recall_k": recall_k,
        "index_fingerprint": idx["fingerprint"],
        "gate": {
            "workspace": workspace,
            "dropped_count": len(dropped),
            "dropped": dropped[:20],
        },
        "modality_quota_notes": quota_notes,
        "figure_ref_boosts": boosted,
        "sufficiency": verdict["verdict"],
        "sufficiency_reasons": verdict.get("reasons", []),
        "retrieve_more": retrieve_more_used,
    }
