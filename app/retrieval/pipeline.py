"""End-to-end retrieval pipeline with a structured per-stage trace.

Stages: recall (bm25 + dense) -> fusion -> filters -> rerank (optional)
-> gates (relevance + anchor/authority) -> sufficiency grader.

Every stage records candidate counts, scores, drop reasons, wall-clock
latency, and a config fingerprint so each result is reproducible and every
kept/dropped evidence item is explainable.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .bm25 import BM25Index
from .dense import DenseRetriever
from .filters import FilterConfig, apply_filters
from .fusion import dense_priority_merge, interleave_merge, rrf_fuse
from .gate import (AnchorGateConfig, RelevanceGateConfig, apply_anchor_gate,
                   apply_relevance_gate)
from .grader import GraderConfig, grade
from .rerank import RerankConfig, apply_rerank
from .types import Candidate

FUSION_DENSE_PRIORITY = "dense_priority"
FUSION_INTERLEAVE = "interleave"
FUSION_RRF = "rrf"


def _lane_trace(candidates: List[Candidate], k: int) -> List[Dict[str, Any]]:
    """Per-record Top-K trace (evidence_id, score, rank, lane provenance,
    filter/gate bookkeeping) so every stage of every case is auditable."""
    return [c.to_trace() for c in candidates[:k]]


@dataclass
class RetrievalConfig:
    name: str = "custom"
    use_bm25: bool = True
    use_dense: bool = True
    recall_k: int = 20
    fusion: str = FUSION_RRF            # dense_priority | rrf
    rrf_k: int = 60
    filters: FilterConfig = field(default_factory=FilterConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    relevance_gate: RelevanceGateConfig = field(default_factory=RelevanceGateConfig)
    anchor_gate: AnchorGateConfig = field(default_factory=AnchorGateConfig)
    grader: GraderConfig = field(default_factory=GraderConfig)
    final_top_k: int = 20

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def config_fingerprint(cfg: RetrievalConfig) -> str:
    return cfg.fingerprint()


class RetrievalPipeline:
    """Wired retrievers + config; call :meth:`run` per query."""

    def __init__(
        self,
        config: RetrievalConfig,
        bm25_index: Optional[BM25Index] = None,
        dense_retriever: Optional[DenseRetriever] = None,
        evidence_items: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.config = config
        self.bm25_index = bm25_index
        self.dense_retriever = dense_retriever
        self._evidence_by_id = {it["evidence_id"]: it
                                for it in (evidence_items or [])}

    # ------------------------------------------------------------------
    def _recall(self, question: str, trace: Dict[str, Any]) -> Dict[str, List[Candidate]]:
        lists: Dict[str, List[Candidate]] = {}
        cfg = self.config
        if cfg.use_bm25:
            if self.bm25_index is None:
                raise RuntimeError("config.use_bm25=True but no BM25Index wired")
            t0 = time.perf_counter()
            lists["bm25"] = self.bm25_index.query(question, top_k=cfg.recall_k)
            trace["stages"]["recall_bm25"] = {
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                "candidates": len(lists["bm25"]),
                "top_scores": [round(c.score, 6) for c in lists["bm25"][:5]],
                "top_k": _lane_trace(lists["bm25"], cfg.recall_k),
            }
        if cfg.use_dense:
            if self.dense_retriever is None:
                raise RuntimeError("config.use_dense=True but no DenseRetriever wired")
            t0 = time.perf_counter()
            lists["dense"] = self.dense_retriever.query(question, k=cfg.recall_k)
            trace["stages"]["recall_dense"] = {
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                "candidates": len(lists["dense"]),
                "top_scores": [round(c.score, 6) for c in lists["dense"][:5]],
                "top_k": _lane_trace(lists["dense"], cfg.recall_k),
            }
        return lists

    # ------------------------------------------------------------------
    def run(self, question: str,
            anchor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = self.config
        started = time.perf_counter()
        trace: Dict[str, Any] = {
            "question": question,
            "config_name": cfg.name,
            "config_fingerprint": cfg.fingerprint(),
            "anchor": anchor,
            "stages": {},
        }

        lists = self._recall(question, trace)

        t0 = time.perf_counter()
        if cfg.fusion == FUSION_RRF:
            fused = rrf_fuse(lists, k=cfg.rrf_k)
        elif cfg.fusion == FUSION_INTERLEAVE:
            fused = interleave_merge(lists, source_order=["dense", "bm25"])
        else:
            fused = dense_priority_merge(lists, primary="dense")
        trace["stages"]["fusion"] = {
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
            "method": cfg.fusion,
            "rrf_k": cfg.rrf_k if cfg.fusion == FUSION_RRF else None,
            "candidates": len(fused),
            "top_k": _lane_trace(fused, cfg.final_top_k),
        }

        t0 = time.perf_counter()
        kept, dropped = apply_filters(fused, self._evidence_by_id, cfg.filters)
        trace["stages"]["filters"] = {
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
            "candidates_in": len(fused),
            "candidates_out": len(kept),
            "dropped": dropped,
        }

        t0 = time.perf_counter()
        reranked = apply_rerank(kept, question, cfg.rerank)
        trace["stages"]["rerank"] = {
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
            "enabled": cfg.rerank.enabled,
            "reranker_type": cfg.rerank.reranker_type if cfg.rerank.enabled else None,
            "proxy_notice": ("token-overlap PROXY, not a cross-encoder"
                             if cfg.rerank.enabled else None),
            "candidates": len(reranked),
            "top_k": _lane_trace(reranked, cfg.final_top_k),
        }

        t0 = time.perf_counter()
        gated, rel_dropped = apply_relevance_gate(reranked, cfg.relevance_gate)
        gated, anchor_notes = apply_anchor_gate(gated, anchor, cfg.anchor_gate)
        gated = gated[: cfg.final_top_k]
        trace["stages"]["gates"] = {
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
            "candidates_in": len(reranked),
            "candidates_out": len(gated),
            "relevance_dropped": rel_dropped,
            "anchor_protection": anchor_notes,
        }

        verdict = grade(gated, cfg.grader)
        trace["stages"]["grader"] = verdict

        trace["total_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        trace["final"] = [c.to_trace() for c in gated]
        trace["grader_verdict"] = verdict["verdict"]
        return trace


# ---------------------------------------------------------------------------
# A0-A4 ablation presets
# ---------------------------------------------------------------------------

def preset_configs() -> Dict[str, RetrievalConfig]:
    """The M4 ablation ladder.

    A0: pure dense top-20 (status-quo baseline; no fusion/rerank/gates).
    A1: BM25 + Dense, round-robin interleave merge (no score fusion).
    A2: A1 with RRF fusion (k=60).
    A3: A2 + deterministic token-overlap proxy rerank.
    A4: A3 + relevance gate + anchor/authority gate.
    """
    a0 = RetrievalConfig(name="A0_dense_only", use_bm25=False, use_dense=True,
                         fusion=FUSION_DENSE_PRIORITY)
    a1 = RetrievalConfig(name="A1_bm25_dense_merge", use_bm25=True, use_dense=True,
                         fusion=FUSION_INTERLEAVE)
    a2 = RetrievalConfig(name="A2_rrf", use_bm25=True, use_dense=True,
                         fusion=FUSION_RRF, rrf_k=60)
    a3 = RetrievalConfig(name="A3_rrf_proxy_rerank", use_bm25=True, use_dense=True,
                         fusion=FUSION_RRF, rrf_k=60,
                         rerank=RerankConfig(enabled=True, top_n=20))
    a4 = RetrievalConfig(name="A4_gates", use_bm25=True, use_dense=True,
                         fusion=FUSION_RRF, rrf_k=60,
                         rerank=RerankConfig(enabled=True, top_n=20),
                         relevance_gate=RelevanceGateConfig(
                             enabled=True, min_score_ratio=0.05),
                         anchor_gate=AnchorGateConfig(enabled=True, protect_top_k=5))
    return {c.name: c for c in (a0, a1, a2, a3, a4)}
