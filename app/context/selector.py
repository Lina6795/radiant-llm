"""Evidence selection under a token budget.

Ranks retrieval candidates by a composite of task relevance (retrieval
score), authority level and freshness, applies a per-page diversity cap
(so one page cannot flood the window with near-duplicate chunks), and
greedily fills the evidence quota.

Pin semantics (the anchor-preservation contract): an item marked
``pinned=True`` is NEVER dropped by selection. Pinned items are admitted
first and consume budget ahead of everything else. If pinned items alone
exceed the evidence budget the selector still keeps all of them and sets
``overflow=True`` — the engine maps that condition to the ``abstain``
branch rather than silently shedding an anchor. Every non-pinned item
that does not make the cut is returned in ``excluded`` with an explicit
reason code; nothing disappears silently.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .tokenizer import TokenCounter, default_counter

REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_DIVERSITY_PAGE_CAP = "diversity_page_cap"
REASON_BELOW_MIN_SCORE = "below_min_score"

AUTHORITY_WEIGHTS: Dict[str, float] = {
    "primary": 1.0,
    "secondary": 0.7,
    "tertiary": 0.4,
}
DEFAULT_AUTHORITY_WEIGHT = 0.5
UNKNOWN_FRESHNESS = 0.5
# Freshness decays with a ~2-year time constant.
FRESHNESS_TAU_SECONDS = 2 * 365 * 24 * 3600


@dataclass
class EvidenceItem:
    evidence_id: str
    content: str
    page: Optional[int] = None
    score: float = 0.0                 # retrieval score, higher is better
    authority_level: str = "primary"
    valid_from: Optional[float] = None  # epoch seconds
    pinned: bool = False
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None

    def tokens(self, counter: Optional[TokenCounter] = None) -> int:
        return (counter or default_counter()).count(self.content)


@dataclass
class SelectionResult:
    selected: List[EvidenceItem] = field(default_factory=list)
    excluded: List[Tuple[EvidenceItem, str]] = field(default_factory=list)
    tokens_used: int = 0
    budget_tokens: int = 0
    overflow: bool = False  # pinned evidence alone exceeds the budget

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected": [it.evidence_id for it in self.selected],
            "excluded": [{"evidence_id": it.evidence_id, "reason": r}
                         for it, r in self.excluded],
            "tokens_used": self.tokens_used,
            "budget_tokens": self.budget_tokens,
            "overflow": self.overflow,
        }


def freshness_weight(valid_from: Optional[float], now: Optional[float]) -> float:
    if valid_from is None:
        return UNKNOWN_FRESHNESS
    now = time.time() if now is None else now
    age = max(0.0, now - valid_from)
    return math.exp(-age / FRESHNESS_TAU_SECONDS)


def composite_scores(
    items: List[EvidenceItem],
    w_rel: float = 0.6,
    w_auth: float = 0.2,
    w_fresh: float = 0.2,
    now: Optional[float] = None,
) -> Dict[str, float]:
    """Score each item in [0, 1]; relevance is normalized within the set."""
    max_score = max((it.score for it in items), default=0.0)
    out: Dict[str, float] = {}
    for it in items:
        rel = (it.score / max_score) if max_score > 0 else 0.0
        auth = AUTHORITY_WEIGHTS.get(it.authority_level,
                                     DEFAULT_AUTHORITY_WEIGHT)
        fresh = freshness_weight(it.valid_from, now)
        out[it.evidence_id] = w_rel * rel + w_auth * auth + w_fresh * fresh
    return out


def select_evidence(
    items: List[EvidenceItem],
    budget_tokens: int,
    counter: Optional[TokenCounter] = None,
    max_per_page: int = 2,
    min_score: float = 0.0,
    now: Optional[float] = None,
) -> SelectionResult:
    """Greedy budgeted selection with pin protection and diversity cap.

    Admission order: all pinned items first (composite-desc), then
    unpinned items by composite score, subject to the per-page cap and
    the remaining budget. The per-page cap counts pinned items too, so
    anchors do not crowd out cross-page coverage of the same page.
    """
    counter = counter or default_counter()
    scores = composite_scores(items, now=now)
    pinned = sorted((it for it in items if it.pinned),
                    key=lambda it: scores[it.evidence_id], reverse=True)
    free = sorted((it for it in items if not it.pinned),
                  key=lambda it: scores[it.evidence_id], reverse=True)

    result = SelectionResult(budget_tokens=budget_tokens)
    page_counts: Dict[int, int] = {}
    used = 0

    for it in pinned:
        result.selected.append(it)
        used += it.tokens(counter)
        if it.page is not None:
            page_counts[it.page] = page_counts.get(it.page, 0) + 1
    if used > budget_tokens:
        result.overflow = True

    for it in free:
        if it.page is not None and page_counts.get(it.page, 0) >= max_per_page:
            result.excluded.append((it, REASON_DIVERSITY_PAGE_CAP))
            continue
        if it.score < min_score:
            result.excluded.append((it, REASON_BELOW_MIN_SCORE))
            continue
        n = it.tokens(counter)
        if used + n > budget_tokens:
            result.excluded.append((it, REASON_BUDGET_EXHAUSTED))
            continue
        result.selected.append(it)
        used += n
        if it.page is not None:
            page_counts[it.page] = page_counts.get(it.page, 0) + 1

    result.tokens_used = used
    return result
