"""Frozen runtime fault-injection cases (benchmarks/runtime_cases.jsonl).

Each case is executed against its scenario driver and evaluated against its
frozen ``expected`` block. The module summary prints the recovery success
rate as JSON so the M3 acceptance gate ("恢复成功率可计算并输出") is
machine-readable from the pytest log.
"""

from __future__ import annotations

import json

import pytest

from tests.durable.conftest import case_ids, load_cases
from tests.durable.scenarios import run_case

CASES = load_cases("runtime_cases.jsonl")
OUTCOMES: dict[str, dict] = {}


@pytest.fixture(scope="module", autouse=True)
def recovery_summary():
    yield
    total = len(OUTCOMES)
    recovered = sum(1 for o in OUTCOMES.values() if o["recovered"])
    rate = recovered / total if total else 0.0
    summary = {
        "total_cases": total,
        "recovered_cases": recovered,
        "recovery_success_rate": rate,
        "per_case": {cid: o["recovered"] for cid, o in sorted(OUTCOMES.items())},
    }
    print(f"\nRUNTIME_CASES_SUMMARY {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")


@pytest.mark.parametrize("case", CASES, ids=case_ids(CASES))
def test_runtime_case(case: dict, tmp_path) -> None:
    outcome = run_case(case, tmp_path)
    OUTCOMES[case["case_id"]] = outcome
    assert outcome["recovered"], (
        f"{case['case_id']} ({case['scenario']}) did not meet expectations: "
        f"{json.dumps(outcome['mismatches'], ensure_ascii=False)}"
    )
