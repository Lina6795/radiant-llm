"""Frozen router contract cases (benchmarks/router_cases.jsonl)."""

from __future__ import annotations

import pytest

from app.control.models import Action, RouterDecision
from app.control.router import RuleRouter
from tests.control.conftest import case_ids, code_matches, load_cases

CASES = load_cases("router_cases.jsonl")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_router_case(case: dict) -> None:
    decision = RuleRouter()(case["input"]["query"])
    assert isinstance(decision, RouterDecision)

    expected = case["expected"]
    assert decision.action.value == expected["action"]
    if "intent" in expected:
        assert decision.intent.value == expected["intent"]
    for code in expected.get("reason_codes_contains", []):
        assert code_matches(decision.reason_codes, code), decision.reason_codes

    contract = expected.get("response_contract")
    if contract:
        if "format" in contract:
            assert decision.response_contract.format.value == contract["format"]
        if "citation_required" in contract:
            assert decision.response_contract.citation_required == contract["citation_required"]

    # Every decision must carry at least one stable reason code.
    assert decision.reason_codes


def test_low_confidence_forces_clarify() -> None:
    router = RuleRouter(confidence_threshold=0.99)
    decision = router("What is the half-life of uranium-235?")
    assert decision.action == Action.CLARIFY
    assert code_matches(decision.reason_codes, "router.low_confidence")
