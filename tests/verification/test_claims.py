"""Claim splitting: determinism, classification, LLM injection + fallback."""

from __future__ import annotations

import json

from app.verification.claims import (
    ClaimType,
    extract_numbers,
    normalize_number,
    numbers_match,
    split_claims,
)

ANSWER = (
    "The Transformer consists of an encoder and a decoder. "
    "The big model achieves 28.4 BLEU on the WMT 2014 English-German task. "
    "Training ran for 100000 steps with 4000 warm-up steps. "
    "Figure 1 shows the encoder on the left and the decoder on the right."
)


def test_split_is_deterministic() -> None:
    a = split_claims(ANSWER)
    b = split_claims(ANSWER)
    assert a.splitter == "rule" == b.splitter
    assert [c.claim_id for c in a.claims] == [c.claim_id for c in b.claims]
    assert [c.text for c in a.claims] == [c.text for c in b.claims]


def test_claim_types() -> None:
    claims = split_claims(ANSWER).claims
    assert claims[0].claim_type is ClaimType.RELATIONAL  # "consists of"
    assert claims[1].claim_type is ClaimType.UNIT        # number + BLEU
    assert claims[2].claim_type is ClaimType.UNIT        # numbers + steps
    assert claims[3].claim_type is ClaimType.VISUAL      # "Figure 1"


def test_number_and_unit_extraction() -> None:
    claims = split_claims(ANSWER).claims
    assert claims[1].numbers == ["28.4", "2014"]
    assert "BLEU" in claims[1].units
    assert claims[2].numbers == ["100000", "4000"]


def test_comma_numbers_normalized() -> None:
    assert extract_numbers("trained for 100,000 steps") == ["100,000"]
    assert normalize_number("100,000") == 100000.0
    assert numbers_match("100,000", "100000")
    assert not numbers_match("28.4", "28.5")


def test_factual_claim() -> None:
    claims = split_claims("Dropout is applied during training.").claims
    assert len(claims) == 1
    assert claims[0].claim_type is ClaimType.FACTUAL


def test_empty_answer_yields_no_claims() -> None:
    result = split_claims("   ")
    assert result.claims == []
    assert result.splitter == "rule"


def test_llm_callable_used_when_healthy() -> None:
    def stub(prompt: str):
        return json.dumps([
            {"text": "The encoder maps inputs to representations.", "claim_type": "factual"},
            {"text": "h equals 8.", "claim_type": "numeric"},
        ])

    result = split_claims(ANSWER, llm_callable=stub)
    assert result.splitter == "llm"
    assert [c.claim_type for c in result.claims] == [ClaimType.FACTUAL, ClaimType.NUMERIC]
    assert result.claims[1].numbers == ["8"]


def test_llm_failure_falls_back_to_rules() -> None:
    def broken(prompt: str):
        raise RuntimeError("endpoint down")

    result = split_claims(ANSWER, llm_callable=broken)
    assert result.splitter == "llm_fallback"
    assert "endpoint down" in result.detail
    assert len(result.claims) == 4  # rule splitter still produced the claims


def test_llm_malformed_payload_falls_back() -> None:
    result = split_claims(ANSWER, llm_callable=lambda p: "not json at all")
    assert result.splitter == "llm_fallback"
    assert result.claims
