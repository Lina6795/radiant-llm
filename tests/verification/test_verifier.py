"""Verifier: numeric/unit/condition checks, coverage, escalation policy."""

from __future__ import annotations

from app.verification.claim_map import map_claims
from app.verification.claims import split_claims
from app.verification.verifier import (
    VERIFY_LOW_AUTHORITY,
    VERIFY_NO_CLAIMS,
    VERIFY_NO_EVIDENCE,
    VERIFY_NUMERIC_MISMATCH,
    VERIFY_OK,
    VERIFY_SOURCE_CONFLICT,
    VERIFY_UNSUPPORTED_CLAIM,
    VerificationAction,
    Verifier,
)

EVIDENCE = [
    {
        "evidence_id": "ev-bleu",
        "document_id": "doc1",
        "page": 8,
        "authority_level": "primary",
        "degraded": False,
        "content": (
            "On the WMT 2014 English-to-German translation task, the big "
            "transformer model achieved a BLEU score of 28.4."
        ),
    },
    {
        "evidence_id": "ev-steps",
        "document_id": "doc1",
        "page": 7,
        "authority_level": "primary",
        "degraded": False,
        "content": "We trained for 100,000 steps with 4000 warm-up steps.",
    },
]


def _verify(answer: str, evidence=EVIDENCE, risk: str = "low", verifier=None):
    claims = split_claims(answer).claims
    cmap = map_claims(claims, evidence)
    v = verifier or Verifier()
    return v.verify(claims=claims, claim_map=cmap, evidence_items=evidence, case_risk=risk)


def test_fully_supported_low_risk_commits() -> None:
    decision = _verify(
        "The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation."
    )
    assert decision.action is VerificationAction.COMMIT
    assert decision.reason_codes == [VERIFY_OK]
    assert decision.numeric_accuracy == 1.0
    assert not decision.unsupported


def test_numeric_mismatch_goes_to_human_review() -> None:
    decision = _verify(
        "The big transformer achieved 31.7 BLEU on WMT 2014 English-to-German translation."
    )
    assert decision.action is VerificationAction.HUMAN_REVIEW
    assert VERIFY_UNSUPPORTED_CLAIM in decision.reason_codes


def test_unit_mismatch_detected() -> None:
    evidence = [
        dict(EVIDENCE[0], content="The big model reached a score of 28.4 points.")
    ]
    claims = split_claims("The big model reached 28.4 BLEU.").claims
    cmap = map_claims(claims, evidence)
    decision = Verifier(min_coverage=0.0).verify(
        claims=claims, claim_map=cmap, evidence_items=evidence, case_risk="low"
    )
    assert any("unit" in code for code in decision.reason_codes)


def test_high_risk_case_with_unsupported_claim_escalates() -> None:
    decision = _verify("The latency is 12 ms on a single A100 GPU.", risk="high")
    assert decision.action is VerificationAction.HUMAN_REVIEW


def test_low_coverage_low_risk_retrieves_more() -> None:
    answer = (
        "The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation. "
        "Quantum chromodynamics confines quarks inside hadrons. "
        "Photosynthesis converts light into chemical energy."
    )
    decision = _verify(answer, risk="low")
    assert decision.action is VerificationAction.RETRIEVE_MORE
    assert decision.coverage < 0.7


def test_conflicting_sources_escalate() -> None:
    conflicting = EVIDENCE + [
        {
            "evidence_id": "ev-alt",
            "document_id": "doc2",
            "page": 2,
            "authority_level": "secondary",
            "degraded": False,
            "content": (
                "On the WMT 2014 English-to-German translation task the big "
                "transformer model achieved a BLEU score of 29.1."
            ),
        }
    ]
    from app.verification.claim_map import ClaimMapper

    claims = split_claims(
        "The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation."
    ).claims
    cmap = ClaimMapper(overlap_threshold=0.05).map(claims, conflicting)
    decision = Verifier().verify(claims=claims, claim_map=cmap, evidence_items=conflicting)
    assert decision.action is VerificationAction.HUMAN_REVIEW
    assert VERIFY_SOURCE_CONFLICT in decision.reason_codes
    assert decision.conflicts


def test_low_authority_escalates() -> None:
    weak = [dict(EVIDENCE[0], authority_level="tertiary")]
    decision = _verify(
        "The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation.",
        evidence=weak,
    )
    assert decision.action is VerificationAction.HUMAN_REVIEW
    assert VERIFY_LOW_AUTHORITY in decision.reason_codes


def test_no_claims_abstains() -> None:
    decision = Verifier().verify(claims=[], claim_map=map_claims([], EVIDENCE), evidence_items=EVIDENCE)
    assert decision.action is VerificationAction.ABSTAIN
    assert decision.reason_codes == [VERIFY_NO_CLAIMS]


def test_no_evidence_abstains() -> None:
    claims = split_claims("The model achieved 28.4 BLEU.").claims
    decision = Verifier().verify(claims=claims, claim_map=map_claims(claims, []), evidence_items=[])
    assert decision.action is VerificationAction.ABSTAIN
    assert decision.reason_codes == [VERIFY_NO_EVIDENCE]


def test_citation_gaps_listed() -> None:
    decision = _verify(
        "The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation. "
        "Quantum chromodynamics confines quarks."
    )
    assert len(decision.citation_gaps) == 1
    assert decision.citation_gaps == decision.unsupported
