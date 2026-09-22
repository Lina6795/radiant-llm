"""Gates: Relevance Gate + Anchor/Authority Gate.

RelevanceGate prunes the tail with an absolute score floor and a
relative-drop rule (a candidate scoring below ``min_score_ratio`` of the
top score is treated as noise).

AnchorGate protects target anchors: when a *target anchor* is known (a
specific page/figure of a specific document — e.g. the section a user is
asking about), a candidate matching that anchor from an authoritative
source must not be crowded out of the kept window by broad high-scoring
chunks from elsewhere. Matching candidates are authority-weighted and, if
present anywhere in the pre-truncation pool, pinned inside the top
``protect_top_k``.

NOTE: in the M4 benchmark the target anchor comes from the case's
``gold_anchor`` for measurement purposes. In production the anchor would
come from request context (open document, cited page, conversation focus),
never from gold labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .types import Candidate

DEFAULT_AUTHORITY_WEIGHTS = {"primary": 1.0, "secondary": 0.9,
                             "tertiary": 0.8, "unknown": 0.7}


@dataclass
class RelevanceGateConfig:
    enabled: bool = False
    min_score: Optional[float] = None       # absolute floor (score-scale dependent)
    min_score_ratio: float = 0.0            # fraction of top score required
    max_drop_ratio: Optional[float] = None  # cut tail at first score[i+1] < ratio * score[i]
    min_keep: int = 1                       # never gate away the last candidate


@dataclass
class AnchorGateConfig:
    enabled: bool = False
    authority_weights: Dict[Optional[str], float] = field(
        default_factory=lambda: dict(DEFAULT_AUTHORITY_WEIGHTS))
    protect_top_k: int = 5     # anchor-matching evidence must survive inside this window
    apply_authority_weight: bool = True


def apply_relevance_gate(
    candidates: List[Candidate],
    cfg: RelevanceGateConfig,
) -> Tuple[List[Candidate], List[Dict[str, Any]]]:
    if not cfg.enabled or not candidates:
        return candidates, []
    kept: List[Candidate] = []
    dropped: List[Dict[str, Any]] = []

    def drop(cand: Candidate, reason: str) -> None:
        cand.kept = False
        cand.gate_notes.append(reason)
        dropped.append({"evidence_id": cand.evidence_id,
                        "rank_before_gate": cand.rank, "reasons": [reason]})

    top_score = candidates[0].score if candidates else 0.0
    for cand in candidates:
        if cfg.min_score is not None and cand.score < cfg.min_score:
            drop(cand, f"below_min_score: {cand.score:.6f} < {cfg.min_score}")
            continue
        if (cfg.min_score_ratio > 0 and top_score > 0
                and cand.score < cfg.min_score_ratio * top_score):
            drop(cand, f"below_relative_floor: {cand.score:.6f} < "
                       f"{cfg.min_score_ratio}*top({top_score:.6f})")
            continue
        kept.append(cand)

    if cfg.max_drop_ratio is not None and len(kept) > 1:
        cut = len(kept)
        for i in range(len(kept) - 1):
            if kept[i].score > 0 and kept[i + 1].score < cfg.max_drop_ratio * kept[i].score:
                cut = i + 1
                break
        for cand in kept[cut:]:
            drop(cand, f"relative_drop_cut: score cliff at rank {cand.rank}")
        kept = kept[:cut]

    if len(kept) < cfg.min_keep and candidates:
        for cand in candidates:
            if cand not in kept:
                cand.kept = True
                cand.gate_notes.append("restored_by_min_keep")
                kept.append(cand)
                dropped[:] = [d for d in dropped if d["evidence_id"] != cand.evidence_id]
                if len(kept) >= cfg.min_keep:
                    break
        kept.sort(key=lambda c: c.rank)

    for rank, cand in enumerate(kept, start=1):
        cand.rank = rank
    return kept, dropped


def _matches_anchor(cand: Candidate, anchor: Dict[str, Any]) -> bool:
    if not anchor:
        return False
    if anchor.get("chunk_id") and cand.chunk_id != anchor["chunk_id"]:
        return False
    if anchor.get("document_id") and cand.document_id != anchor["document_id"]:
        return False
    if anchor.get("page") is not None and cand.page != anchor["page"]:
        return False
    return True


def apply_anchor_gate(
    candidates: List[Candidate],
    anchor: Optional[Dict[str, Any]],
    cfg: AnchorGateConfig,
) -> Tuple[List[Candidate], List[Dict[str, Any]]]:
    """Authority-weight scores, then pin anchor matches inside protect_top_k."""
    notes: List[Dict[str, Any]] = []
    if not cfg.enabled or not candidates:
        return candidates, notes

    if cfg.apply_authority_weight:
        changed = False
        for cand in candidates:
            w = cfg.authority_weights.get(
                cand.authority_level or "unknown",
                cfg.authority_weights.get("unknown", 1.0))
            if w != 1.0:
                cand.score *= w
                changed = True
                cand.gate_notes.append(f"authority_weight={w}")
        if changed:
            candidates = sorted(candidates,
                                key=lambda c: (-c.score, c.rank, c.evidence_id))
            for rank, cand in enumerate(candidates, start=1):
                cand.rank = rank

    if anchor:
        protected = [c for c in candidates
                     if c.rank > cfg.protect_top_k
                     and _matches_anchor(c, anchor)]
        for cand in protected:
            # Swap with the last non-anchor candidate inside the window so
            # every anchor match fits (earlier protections are not pushed
            # back out when several candidates match the same anchor).
            swap_at = None
            for j in range(min(cfg.protect_top_k, len(candidates)) - 1, -1, -1):
                if not _matches_anchor(candidates[j], anchor):
                    swap_at = j
                    break
            if swap_at is None:
                break  # window already full of anchor matches
            pos = next(i for i, c in enumerate(candidates) if c is cand)
            candidates[pos], candidates[swap_at] = candidates[swap_at], candidates[pos]
            cand.gate_notes.append(
                f"anchor_protected: promoted into top-{cfg.protect_top_k}")
            notes.append({"evidence_id": cand.evidence_id,
                          "action": "promoted",
                          "reason": "matches target anchor; protected from "
                                    "being crowded out by broad sources"})
        for rank, cand in enumerate(candidates, start=1):
            cand.rank = rank
    return candidates, notes
