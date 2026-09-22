"""Reranking stage with a Cross-Encoder-style interface.

The interface mirrors a cross-encoder: ``score(query, candidate_contents)``
-> one relevance score per candidate. The default configuration is
``enabled=False`` — no reranker runs and no model is loaded.

``TokenOverlapReranker`` is a **deterministic PROXY, not a real
cross-encoder**: it scores BM25-like token overlap between query and
candidate content. It exists so the A3 ablation can measure rerank-stage
wiring on CPU without downloading any model. A real cross-encoder
implementation would plug in behind the same ``Reranker`` protocol; none is
bundled here (deliberately — this environment forbids large model downloads).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Protocol

from .types import Candidate, tokenize


@dataclass
class RerankConfig:
    enabled: bool = False
    reranker_type: str = "token_overlap_proxy"  # only proxy bundled
    top_n: int = 20        # rerank at most this many head candidates
    idf_floor: float = 0.1


class Reranker(Protocol):
    def score(self, query: str, contents: List[str]) -> List[float]:
        ...


class TokenOverlapReranker:
    """Deterministic proxy scorer: IDF-weighted token overlap.

    PROXY — NOT a cross-encoder. Uses corpus IDF estimated from the
    candidate contents themselves; fully deterministic, no I/O, no model.
    """

    def __init__(self, idf_floor: float = 0.1) -> None:
        self.idf_floor = idf_floor

    def score(self, query: str, contents: List[str]) -> List[float]:
        q_terms = set(tokenize(query))
        if not q_terms or not contents:
            return [0.0] * len(contents)
        docs = [tokenize(c) for c in contents]
        n = len(docs)
        df: Dict[str, int] = {}
        for terms in docs:
            for t in set(terms):
                df[t] = df.get(t, 0) + 1
        idf = {t: max(self.idf_floor, math.log(1.0 + (n - d + 0.5) / (d + 0.5)))
               for t, d in df.items()}
        scores: List[float] = []
        for terms in docs:
            if not terms:
                scores.append(0.0)
                continue
            counts: Dict[str, int] = {}
            for t in terms:
                counts[t] = counts.get(t, 0) + 1
            total = sum(idf.get(t, self.idf_floor) * counts[t]
                        for t in q_terms if t in counts)
            # Length-normalised so long broad chunks do not auto-win.
            scores.append(total / math.sqrt(len(terms)))
        return scores


def build_reranker(cfg: RerankConfig):
    if not cfg.enabled:
        return None
    if cfg.reranker_type != "token_overlap_proxy":
        raise ValueError(
            f"unknown reranker_type {cfg.reranker_type!r}; only "
            "'token_overlap_proxy' (deterministic proxy) is bundled")
    return TokenOverlapReranker(idf_floor=cfg.idf_floor)


def apply_rerank(candidates: List[Candidate], query: str,
                 cfg: RerankConfig) -> List[Candidate]:
    """Re-order the top-N head by proxy score; tail keeps prior order.

    Pre-rerank rank/score are preserved in ``source_ranks['pre_rerank']``.
    """
    if not cfg.enabled or not candidates:
        return candidates
    reranker = build_reranker(cfg)
    head, tail = candidates[: cfg.top_n], candidates[cfg.top_n:]
    for cand in head:
        cand.source_ranks.setdefault("pre_rerank",
                                     {"rank": cand.rank, "score": cand.score})
    proxy_scores = reranker.score(query, [c.content for c in head])
    for cand, ps in zip(head, proxy_scores):
        cand.score = ps  # head is re-scored; fused score kept under pre_rerank
        cand.raw_score = ps
        cand.source = "reranked"
    head = [c for _, c in sorted(
        zip(proxy_scores, head),
        key=lambda pc: (-pc[0], pc[1].source_ranks["pre_rerank"]["rank"],
                        pc[1].evidence_id))]
    out = head + tail
    for rank, cand in enumerate(out, start=1):
        cand.rank = rank
    return out
