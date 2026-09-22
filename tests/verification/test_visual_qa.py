"""Visual parse QA: synthetic fixtures only (real VLM output is deferred)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.verification.visual_qa import (
    IssueType,
    check_record,
    check_records,
    description_entropy,
    digit_density,
    is_low_information,
)

FIXTURES = Path(__file__).resolve().parents[2] / "benchmarks" / "visual_cases.jsonl"


def _qa_fixtures():
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [c for c in cases if c["kind"] == "qa_fixture"]


def test_metric_primitives() -> None:
    assert description_entropy("") == 0.0
    assert description_entropy("aaaa") < description_entropy("ab1?")
    assert digit_density("") == 0.0
    assert digit_density("a1b2") == 0.5


def test_low_information_rules() -> None:
    low, _ = is_low_information("a figure")
    assert low
    low, why = is_low_information("x" * 100)
    assert low and "entropy" in why
    low, why = is_low_information(
        "A bar chart comparing the models along the validation axis with error bars shown clearly."
    )
    assert low and "digit density" in why
    ok, _ = is_low_information(
        "Bar chart of BLEU scores 25.8, 26.3 and 28.4 across three model variants with error bars."
    )
    assert not ok


def test_synthetic_fixtures_flag_expected_issues() -> None:
    fixtures = _qa_fixtures()
    assert len(fixtures) >= 6
    report = check_records(
        [f["record"] for f in fixtures],
        page_counts={
            str(f["record"]["source"]): f["page_count"]
            for f in fixtures
            if "page_count" in f
        },
    )
    by_case = {}
    for f in fixtures:
        key = f["record"].get("figure_id") or f"{f['record']['source']}"
        by_case[f["case_id"]] = (key, set(f["expect_issues"]))

    issues_by_key: dict[str, set[str]] = {}
    for issue in report.issues:
        issues_by_key.setdefault(issue.record_key, set()).add(issue.issue_type.value)

    for case_id, (key, expected) in by_case.items():
        got = issues_by_key.get(key, set())
        assert expected <= got, f"{case_id}: expected {expected}, got {got}"


def test_clean_record_passes() -> None:
    fixtures = _qa_fixtures()
    ok = next(f for f in fixtures if f["case_id"] == "VQ-OK01")
    assert check_record(ok["record"]) == []


def test_issue_counts_aggregated() -> None:
    fixtures = _qa_fixtures()
    report = check_records([f["record"] for f in fixtures])
    assert report.records_checked == len(fixtures)
    assert report.issue_counts.get("empty_description", 0) >= 1
    assert report.issue_counts.get("wrong_page", 0) >= 1
    assert report.issue_counts.get("wrong_figure_index", 0) >= 2
    assert report.issue_counts.get("low_information", 0) >= 2
    assert not report.clean
