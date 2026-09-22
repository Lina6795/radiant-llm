"""RADIANT-Control M7: Verifier.

Checks an answer's atomic claims against the Claim-Evidence Map:

* numeric consistency  — claim numbers must appear in the bound evidence
* unit consistency     — claim units must appear in the bound evidence
* applicability        — claim entities (conditions like "WMT 2014",
  "English-German") must appear in the bound evidence, so a number that is
  right for the wrong condition is still caught
* source conflict      — propagated from the Claim-Evidence Map
* citation coverage    — fraction of claims bound to >=1 evidence item
* authority            — bound evidence below the accepted authority level

Escalation policy: conflicts, numeric/unit mismatches, and any unsupported
or mismatched claim on a medium/high-risk case go to ``human_review``.
Low coverage without conflicts goes to ``retrieve_more`` first. No claims
or no evidence at all -> ``abstain``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .claim_map import BindingStatus, ClaimMap
from .claims import Claim, ClaimType, extract_units

# Reason codes (stable strings; prefixed "verify.*" to sit next to the
# M2 control-plane codes without modifying app/control).
VERIFY_OK = "verify.ok"
VERIFY_NUMERIC_MISMATCH = "verify.numeric_mismatch"
VERIFY_UNIT_MISMATCH = "verify.unit_mismatch"
VERIFY_CONDITION_MISMATCH = "verify.condition_mismatch"
VERIFY_SOURCE_CONFLICT = "verify.source_conflict"
VERIFY_LOW_COVERAGE = "verify.low_citation_coverage"
VERIFY_UNSUPPORTED_CLAIM = "verify.unsupported_claim"
VERIFY_LOW_AUTHORITY = "verify.low_authority"
VERIFY_HIGH_RISK = "verify.high_risk_case"
VERIFY_NO_CLAIMS = "verify.no_claims"
VERIFY_NO_EVIDENCE = "verify.no_evidence"


class VerificationAction(str, Enum):
    COMMIT = "commit"
    RETRIEVE_MORE = "retrieve_more"
    CLARIFY = "clarify"
    HUMAN_REVIEW = "human_review"
    ABSTAIN = "abstain"


class ClaimCheck(BaseModel):
    claim_id: str
    passed: bool
    codes: List[str] = Field(default_factory=list)
    detail: str = ""


class VerificationDecision(BaseModel):
    action: VerificationAction
    supported: List[str] = Field(default_factory=list)
    unsupported: List[str] = Field(default_factory=list)
    citation_gaps: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    reason_codes: List[str] = Field(default_factory=list)
    coverage: float = 0.0
    numeric_accuracy: Optional[float] = None
    checks: List[ClaimCheck] = Field(default_factory=list)
    rationale: str = ""


class Verifier:
    def __init__(
        self,
        *,
        min_coverage: float = 0.7,
        high_risk_levels: tuple[str, ...] = ("medium", "high"),
        accepted_authority: tuple[str, ...] = ("primary", "secondary"),
    ) -> None:
        self.min_coverage = min_coverage
        self.high_risk_levels = high_risk_levels
        self.accepted_authority = accepted_authority

    # ------------------------------------------------------------------
    def _check_claim(
        self,
        claim: Claim,
        cmap: ClaimMap,
        evidence_by_id: Dict[str, Dict[str, Any]],
    ) -> ClaimCheck:
        binding = next((b for b in cmap.bindings if b.claim_id == claim.claim_id), None)
        if binding is None or binding.status is BindingStatus.UNSUPPORTED:
            return ClaimCheck(
                claim_id=claim.claim_id, passed=False,
                codes=[VERIFY_UNSUPPORTED_CLAIM], detail="claim has no supporting evidence",
            )
        if binding.status is BindingStatus.CONFLICTED:
            return ClaimCheck(
                claim_id=claim.claim_id, passed=False,
                codes=[VERIFY_SOURCE_CONFLICT], detail=binding.reason,
            )

        codes: List[str] = []
        evidence = evidence_by_id.get(binding.evidence_id or "", {})
        content = (evidence.get("content") or "").replace(",", "")

        if claim.claim_type in (ClaimType.NUMERIC, ClaimType.UNIT):
            missing = [n for n in claim.numbers if n.replace(",", "") not in content]
            if missing:
                codes.append(VERIFY_NUMERIC_MISMATCH)
        if claim.units:
            missing_units = [
                u for u in claim.units if u not in extract_units(evidence.get("content") or "")
            ]
            if missing_units:
                codes.append(VERIFY_UNIT_MISMATCH)
        # Applicability: entity conditions in the claim must be verifiable
        # in the same evidence; guards against right-number-wrong-condition.
        missing_entities = [
            e for e in claim.entities if e.lower() not in (evidence.get("content") or "").lower()
        ]
        if claim.entities and len(missing_entities) == len(claim.entities):
            codes.append(VERIFY_CONDITION_MISMATCH)

        authority = (binding.authority_level or evidence.get("authority_level") or "")
        if authority and authority not in self.accepted_authority:
            codes.append(VERIFY_LOW_AUTHORITY)

        return ClaimCheck(
            claim_id=claim.claim_id,
            passed=not codes,
            codes=codes,
            detail="; ".join(codes) if codes else "claim consistent with bound evidence",
        )

    # ------------------------------------------------------------------
    def verify(
        self,
        *,
        claims: List[Claim],
        claim_map: ClaimMap,
        evidence_items: List[Dict[str, Any]],
        case_risk: str = "low",
    ) -> VerificationDecision:
        if not claims:
            return VerificationDecision(
                action=VerificationAction.ABSTAIN,
                reason_codes=[VERIFY_NO_CLAIMS],
                rationale="answer produced no verifiable claims",
            )
        if not evidence_items:
            return VerificationDecision(
                action=VerificationAction.ABSTAIN,
                reason_codes=[VERIFY_NO_EVIDENCE],
                coverage=0.0,
                rationale="no evidence retrieved; nothing to verify against",
            )

        evidence_by_id = {it.get("evidence_id"): it for it in evidence_items}
        checks = [self._check_claim(c, claim_map, evidence_by_id) for c in claims]

        supported = [c.claim_id for c in checks if c.passed]
        unsupported = list(claim_map.unsupported)
        citation_gaps = list(claim_map.unsupported)
        conflicts = list(claim_map.conflicted)
        failed_codes = sorted({code for chk in checks for code in chk.codes})

        numeric_claims = [
            c for c in claims if c.claim_type in (ClaimType.NUMERIC, ClaimType.UNIT)
        ]
        numeric_ok = [
            chk for chk in checks
            if chk.claim_id in {c.claim_id for c in numeric_claims} and chk.passed
        ]
        numeric_accuracy = (
            round(len(numeric_ok) / len(numeric_claims), 4) if numeric_claims else None
        )

        high_risk = case_risk in self.high_risk_levels
        hard_fail = (
            conflicts
            or VERIFY_NUMERIC_MISMATCH in failed_codes
            or VERIFY_UNIT_MISMATCH in failed_codes
            or VERIFY_CONDITION_MISMATCH in failed_codes
            or VERIFY_LOW_AUTHORITY in failed_codes
        )
        # Contradiction: a claim bound to well-overlapping evidence whose
        # numbers are absent there — the corpus disagrees, not just misses.
        bound_ids = {b.claim_id for b in claim_map.bindings if b.evidence_id}
        contradiction = any(cid in bound_ids for cid in unsupported)

        if hard_fail or contradiction:
            action = VerificationAction.HUMAN_REVIEW
        elif high_risk and unsupported:
            action = VerificationAction.HUMAN_REVIEW
        elif claim_map.coverage < self.min_coverage:
            action = VerificationAction.RETRIEVE_MORE
        elif unsupported:
            action = VerificationAction.HUMAN_REVIEW
        else:
            action = VerificationAction.COMMIT

        codes = list(failed_codes)
        if claim_map.coverage < self.min_coverage:
            codes.append(VERIFY_LOW_COVERAGE)
        if high_risk and action is VerificationAction.HUMAN_REVIEW:
            codes.append(VERIFY_HIGH_RISK)
        if not codes:
            codes = [VERIFY_OK]

        return VerificationDecision(
            action=action,
            supported=supported,
            unsupported=unsupported,
            citation_gaps=citation_gaps,
            conflicts=conflicts,
            reason_codes=codes,
            coverage=claim_map.coverage,
            numeric_accuracy=numeric_accuracy,
            checks=checks,
            rationale=(
                f"coverage={claim_map.coverage:.2f} supported={len(supported)} "
                f"unsupported={len(unsupported)} conflicts={len(conflicts)} "
                f"risk={case_risk} -> {action.value}"
            ),
        )
