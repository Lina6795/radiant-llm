"""Frozen-case sanity check for benchmarks/context_cases.jsonl."""

import json
from pathlib import Path

CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "context_cases.jsonl"

REQUIRED = {"case_id", "category", "scenario", "inputs", "expected", "source"}
CATEGORIES = {"branch", "anchor_preservation", "isolation",
              "compression_redline", "compression_lineage", "budget_model",
              "tokenizer", "no_silent_truncation"}


def load_cases():
    with open(CASES, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_at_least_ten_frozen_cases():
    assert len(load_cases()) >= 10


def test_case_ids_unique_and_fields_present():
    cases = load_cases()
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        assert REQUIRED <= set(case), case["case_id"]
        assert case["category"] in CATEGORIES
        assert case["case_id"].startswith("CTX-")


def test_branch_cases_cover_all_four_overflow_branches():
    expected_decisions = set()
    for case in load_cases():
        decision = case["expected"].get("decision")
        if decision:
            expected_decisions.add(decision)
    assert {"assemble", "compress", "retrieve_more", "clarify",
            "abstain"} <= expected_decisions
