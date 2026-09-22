"""Evidence Sufficiency Grader (rule-based).

Verdicts:
- ``enough``        — at least ``min_evidence`` kept candidates and the top
                      score clears ``min_top_score``, and no conflict found.
- ``retrieve_more`` — some evidence but below count/score requirements.
- ``conflict``      — multiple high-scoring candidates sit on the same page
                      of the same document with near-zero content overlap
                      (rule-of-thumb proxy for mutually inconsistent claims
                      from the same location).
- ``abstain``       — nothing usable survived the gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .types import Candidate, tokenize

ENOUGH = "enough"
RETRIEVE_MORE = "retrieve_more"
CONFLICT = "conflict"
ABSTAIN = "abstain"


@dataclass
class GraderConfig:
    min_evidence: int = 2
    min_top_score: float = 0.0
    conflict_top_n: int = 5            # inspect this many head candidates
    conflict_overlap_threshold: float = 0.05  # Jaccard below this = divergent


def _jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def grade(candidates: List[Candidate], cfg: GraderConfig) -> Dict[str, Any]:
    kept = [c for c in candidates if c.kept]
    detail: Dict[str, Any] = {
        "verdict": None,
        "kept_count": len(kept),
        "top_score": kept[0].score if kept else None,
        "reasons": [],
        "conflicts": [],
    }
    if not kept:
        detail["verdict"] = ABSTAIN
        detail["reasons"].append("no candidates survived filters/gates")
        return detail

    # Same-page conflict heuristic on the head.
    head = kept[: cfg.conflict_top_n]
    strong = [c for c in head if kept[0].score <= 0 or c.score >= 0.5 * kept[0].score]
    for i in range(len(strong)):
        for j in range(i + 1, len(strong)):
            a, b = strong[i], strong[j]
            if (a.document_id and a.document_id == b.document_id
                    and a.page is not None and a.page == b.page):
                overlap = _jaccard(a.content, b.content)
                if overlap < cfg.conflict_overlap_threshold:
                    detail["conflicts"].append({
                        "page": a.page,
                        "evidence_ids": [a.evidence_id, b.evidence_id],
                        "token_overlap": round(overlap, 4),
                    })

    top_score_ok = kept[0].score >= cfg.min_top_score
    count_ok = len(kept) >= cfg.min_evidence

    if detail["conflicts"] and top_score_ok:
        detail["verdict"] = CONFLICT
        detail["reasons"].append(
            f"{len(detail['conflicts'])} same-page divergent evidence pair(s)")
        return detail
    if count_ok and top_score_ok:
        detail["verdict"] = ENOUGH
        detail["reasons"].append(
            f"{len(kept)} candidates >= {cfg.min_evidence}, "
            f"top score {kept[0].score:.6f} >= {cfg.min_top_score}")
        return detail
    detail["verdict"] = RETRIEVE_MORE
    if not count_ok:
        detail["reasons"].append(
            f"only {len(kept)} candidates (< {cfg.min_evidence})")
    if not top_score_ok:
        detail["reasons"].append(
            f"top score {kept[0].score:.6f} < {cfg.min_top_score}")
    return detail
