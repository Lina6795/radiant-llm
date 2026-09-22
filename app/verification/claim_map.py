"""RADIANT-Control M7: Claim-Evidence Map.

Binds every atomic claim to the evidence that supports it (evidence_id +
page), or marks it unsupported. Each binding carries a confidence score
and a human-readable reason so every claim is auditable.

Scoring is deterministic: content-token overlap (Jaccard over
non-stopword tokens) plus a numeric-presence bonus for numeric/unit
claims. Conflicts are flagged when the two best evidence items both
overlap well but carry disjoint numeric values.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .claims import Claim, ClaimType, content_tokens, extract_numbers


class BindingStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTED = "conflicted"


class ClaimBinding(BaseModel):
    claim_id: str
    claim_text: str
    claim_type: ClaimType
    status: BindingStatus
    evidence_id: Optional[str] = None
    page: Optional[int] = None
    document_id: Optional[str] = None
    authority_level: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    candidate_ids: List[str] = Field(default_factory=list)


class ClaimMap(BaseModel):
    bindings: List[ClaimBinding]
    coverage: float  # fraction of claims bound to >=1 evidence item
    supported: List[str]
    unsupported: List[str]
    conflicted: List[str]


def overlap_score(claim_tokens: set[str], evidence_content: str) -> float:
    ev_tokens = content_tokens(evidence_content)
    if not claim_tokens or not ev_tokens:
        return 0.0
    return len(claim_tokens & ev_tokens) / len(claim_tokens | ev_tokens)


def _numbers_present(numbers: List[str], content: str) -> List[str]:
    """Claim numbers literally present in the evidence content, after
    comma-normalization on both sides."""
    flat = (content or "").replace(",", "")
    return [n for n in numbers if n.replace(",", "") in flat]


class ClaimMapper:
    def __init__(
        self,
        *,
        overlap_threshold: float = 0.08,
        top_n: int = 3,
        numeric_bonus: float = 0.25,
    ) -> None:
        self.overlap_threshold = overlap_threshold
        self.top_n = top_n
        self.numeric_bonus = numeric_bonus

    def _score(self, claim: Claim, item: Dict[str, Any]) -> tuple[float, str]:
        content = item.get("content") or ""
        base = overlap_score(content_tokens(claim.text), content)
        note = ""
        if claim.claim_type in (ClaimType.NUMERIC, ClaimType.UNIT) and claim.numbers:
            present = _numbers_present(claim.numbers, content)
            if present:
                base += self.numeric_bonus * len(present) / len(claim.numbers)
                note = f"numbers {present} present in evidence"
            else:
                note = "claim numbers absent from evidence"
        return base, note

    def _detect_conflict(
        self, claim: Claim, ranked: List[tuple[float, Dict[str, Any]]]
    ) -> bool:
        """Potential source conflict: the best well-overlapping evidence
        confirms the claim's key number, but a second well-overlapping
        item does not contain it and carries a number neither the claim
        nor the best item has (i.e. a different value for the same slot)."""
        if claim.claim_type not in (ClaimType.NUMERIC, ClaimType.UNIT) or not claim.numbers:
            return False
        good = [it for s, it in ranked[:2] if s >= self.overlap_threshold]
        if len(good) < 2:
            return False
        first, second = good
        key = claim.numbers[0].replace(",", "")
        first_content = (first.get("content") or "").replace(",", "")
        second_content = (second.get("content") or "").replace(",", "")
        if key not in first_content or key in second_content:
            return False
        claim_numbers = {n.replace(",", "") for n in claim.numbers}
        first_numbers = {n.replace(",", "") for n in extract_numbers(first.get("content") or "")}
        extras = [
            n for n in extract_numbers(second.get("content") or "")
            if n.replace(",", "") not in claim_numbers | first_numbers
        ]
        return bool(extras)

    def bind(self, claim: Claim, evidence_items: List[Dict[str, Any]]) -> ClaimBinding:
        scored: List[tuple[float, Dict[str, Any], str]] = []
        for item in evidence_items:
            if item.get("degraded"):
                continue
            score, note = self._score(claim, item)
            scored.append((score, item, note))
        scored.sort(key=lambda t: t[0], reverse=True)
        ranked = [(s, it) for s, it, _ in scored]
        candidates = [it.get("evidence_id", "") for s, it in ranked[: self.top_n] if s > 0]

        if not ranked or ranked[0][0] < self.overlap_threshold:
            return ClaimBinding(
                claim_id=claim.claim_id,
                claim_text=claim.text,
                claim_type=claim.claim_type,
                status=BindingStatus.UNSUPPORTED,
                confidence=round(ranked[0][0], 4) if ranked else 0.0,
                reason="no evidence above overlap threshold",
                candidate_ids=candidates,
            )

        best_score, best, note = scored[0]
        page = best.get("page")
        try:
            page = int(page) if page is not None else None
        except (TypeError, ValueError):
            page = None

        if self._detect_conflict(claim, ranked):
            return ClaimBinding(
                claim_id=claim.claim_id,
                claim_text=claim.text,
                claim_type=claim.claim_type,
                status=BindingStatus.CONFLICTED,
                evidence_id=best.get("evidence_id"),
                page=page,
                document_id=best.get("document_id"),
                authority_level=best.get("authority_level"),
                confidence=round(min(1.0, best_score), 4),
                reason="top evidence items carry disjoint numeric values",
                candidate_ids=candidates,
            )

        numeric_ok = True
        missing_note = note
        if claim.claim_type in (ClaimType.NUMERIC, ClaimType.UNIT) and claim.numbers:
            present = _numbers_present(claim.numbers, best.get("content") or "")
            numeric_ok = len(present) == len(claim.numbers)
            if not numeric_ok:
                absent = [n for n in claim.numbers if n not in present]
                missing_note = f"claim numbers {absent} absent from best evidence"
        if not numeric_ok:
            return ClaimBinding(
                claim_id=claim.claim_id,
                claim_text=claim.text,
                claim_type=claim.claim_type,
                status=BindingStatus.UNSUPPORTED,
                evidence_id=best.get("evidence_id"),
                page=page,
                document_id=best.get("document_id"),
                authority_level=best.get("authority_level"),
                confidence=round(min(1.0, best_score), 4),
                reason=missing_note,
                candidate_ids=candidates,
            )

        return ClaimBinding(
            claim_id=claim.claim_id,
            claim_text=claim.text,
            claim_type=claim.claim_type,
            status=BindingStatus.SUPPORTED,
            evidence_id=best.get("evidence_id"),
            page=page,
            document_id=best.get("document_id"),
            authority_level=best.get("authority_level"),
            confidence=round(min(1.0, best_score), 4),
            reason=note or "token overlap above threshold",
            candidate_ids=candidates,
        )

    def map(self, claims: List[Claim], evidence_items: List[Dict[str, Any]]) -> ClaimMap:
        bindings = [self.bind(c, evidence_items) for c in claims]
        bound = [b for b in bindings if b.status is not BindingStatus.UNSUPPORTED]
        return ClaimMap(
            bindings=bindings,
            coverage=round(len(bound) / len(bindings), 4) if bindings else 0.0,
            supported=[b.claim_id for b in bindings if b.status is BindingStatus.SUPPORTED],
            unsupported=[b.claim_id for b in bindings if b.status is BindingStatus.UNSUPPORTED],
            conflicted=[b.claim_id for b in bindings if b.status is BindingStatus.CONFLICTED],
        )


def map_claims(
    claims: List[Claim],
    evidence_items: List[Dict[str, Any]],
    **kwargs: Any,
) -> ClaimMap:
    return ClaimMapper(**kwargs).map(claims, evidence_items)
