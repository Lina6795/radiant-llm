"""Claim-Evidence Map: binding, unsupported marking, conflicts, coverage."""

from __future__ import annotations

from app.verification.claim_map import BindingStatus, ClaimMapper, map_claims
from app.verification.claims import split_claims

EVIDENCE = [
    {
        "evidence_id": "ev-bleu",
        "document_id": "doc1",
        "page": 8,
        "authority_level": "primary",
        "degraded": False,
        "content": (
            "On the WMT 2014 English-to-German translation task, the big "
            "transformer model achieved a BLEU score of 28.4. The base model "
            "reached 27.3 BLEU."
        ),
    },
    {
        "evidence_id": "ev-steps",
        "document_id": "doc1",
        "page": 7,
        "authority_level": "primary",
        "degraded": False,
        "content": (
            "We trained for 100,000 steps or 12 hours. The learning rate "
            "schedule uses 4000 warm-up steps."
        ),
    },
]


def _claims(text: str):
    return split_claims(text).claims


def test_supported_binding_has_evidence_and_page() -> None:
    claims = _claims("The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German.")
    cmap = map_claims(claims, EVIDENCE)
    binding = cmap.bindings[0]
    assert binding.status is BindingStatus.SUPPORTED
    assert binding.evidence_id == "ev-bleu"
    assert binding.page == 8
    assert binding.confidence > 0
    assert cmap.coverage == 1.0
    assert cmap.supported == [binding.claim_id]


def test_comma_normalized_number_match() -> None:
    claims = _claims("Training ran for 100000 steps.")
    cmap = map_claims(claims, EVIDENCE)
    assert cmap.bindings[0].status is BindingStatus.SUPPORTED
    assert cmap.bindings[0].evidence_id == "ev-steps"


def test_numeric_claim_with_absent_number_is_unsupported() -> None:
    claims = _claims("The model achieved 31.7 BLEU on WMT 2014 English-to-German translation.")
    cmap = map_claims(claims, EVIDENCE)
    binding = cmap.bindings[0]
    assert binding.status is BindingStatus.UNSUPPORTED
    assert "absent" in binding.reason
    assert cmap.unsupported == [binding.claim_id]


def test_no_overlap_is_unsupported() -> None:
    claims = _claims("Quantum chromodynamics predicts confinement of quarks.")
    cmap = map_claims(claims, EVIDENCE)
    assert cmap.bindings[0].status is BindingStatus.UNSUPPORTED
    assert cmap.bindings[0].evidence_id is None
    assert cmap.coverage == 0.0


def test_conflicting_sources_flagged() -> None:
    conflicting = EVIDENCE + [
        {
            "evidence_id": "ev-bleu-alt",
            "document_id": "doc2",
            "page": 2,
            "authority_level": "secondary",
            "degraded": False,
            "content": (
                "On the WMT 2014 English-to-German translation task the big "
                "transformer model achieved a BLEU score of 29.1 in the "
                "replication run."
            ),
        }
    ]
    claims = _claims("The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German translation.")
    cmap = ClaimMapper(overlap_threshold=0.05).map(claims, conflicting)
    assert cmap.bindings[0].status is BindingStatus.CONFLICTED
    assert cmap.conflicted == [cmap.bindings[0].claim_id]


def test_degraded_evidence_never_bound() -> None:
    degraded_only = [dict(EVIDENCE[0], degraded=True)]
    claims = _claims("The big transformer achieved 28.4 BLEU on WMT 2014 English-to-German.")
    cmap = map_claims(claims, degraded_only)
    assert cmap.bindings[0].status is BindingStatus.UNSUPPORTED
    assert cmap.bindings[0].evidence_id is None


def test_empty_claims_and_empty_evidence() -> None:
    assert map_claims([], EVIDENCE).coverage == 0.0
    claims = _claims("The big transformer achieved 28.4 BLEU.")
    assert map_claims(claims, []).bindings[0].status is BindingStatus.UNSUPPORTED
