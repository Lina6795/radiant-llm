"""Visual eval scaffold: metric math on synthetic fixtures, deferred path."""

from __future__ import annotations

import json
from pathlib import Path

from app.verification.visual_eval import (
    bbox_iou,
    evaluate_visual_retrieval,
    recall_at_k,
    region_recall_at_k,
    run_visual_eval,
)

FIXTURES = Path(__file__).resolve().parents[2] / "benchmarks" / "visual_cases.jsonl"


def _eval_fixtures():
    cases = [
        json.loads(line)
        for line in FIXTURES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [c for c in cases if c["kind"] == "eval_fixture"]


def test_recall_at_k_math() -> None:
    gold = {"fig-a", "fig-b"}
    assert recall_at_k(["fig-x", "fig-a"], gold, 1) == 0.0
    assert recall_at_k(["fig-x", "fig-a"], gold, 2) == 0.5
    assert recall_at_k(["fig-a", "fig-b"], gold, 2) == 1.0
    assert recall_at_k([], gold, 5) == 0.0
    assert recall_at_k(["fig-a"], set(), 5) == 0.0


def test_eval_fixture_recall() -> None:
    case = next(c for c in _eval_fixtures() if c["case_id"] == "VE-EVAL01")
    assert recall_at_k(case["ranked_candidates"], set(case["gold_figure_ids"]), 1) == case["expect_recall_at_1"]
    assert recall_at_k(case["ranked_candidates"], set(case["gold_figure_ids"]), 3) == case["expect_recall_at_3"]


def test_bbox_iou() -> None:
    a = {"x0": 0, "y0": 0, "x1": 10, "y1": 10}
    assert bbox_iou(a, a) == 1.0
    assert bbox_iou(a, {"x0": 20, "y0": 20, "x1": 30, "y1": 30}) == 0.0
    assert 0.0 < bbox_iou(a, {"x0": 5, "y0": 5, "x1": 15, "y1": 15}) < 1.0


def test_region_recall_fixture() -> None:
    case = next(c for c in _eval_fixtures() if c["case_id"] == "VE-EVAL02")
    got = region_recall_at_k(
        case["ranked_regions"], case["gold_regions"], 2,
        iou_threshold=case["iou_threshold"],
    )
    assert got == case["expect_region_recall_at_2"]
    # stricter IoU rejects the shifted candidate
    assert region_recall_at_k(case["ranked_regions"], case["gold_regions"], 2, iou_threshold=0.9) == 0.0


def test_aggregate_counts_missing_results() -> None:
    out = evaluate_visual_retrieval(
        results={"c1": ["fig-a"]},
        gold={"c1": ["fig-a"], "c2": ["fig-b"]},
        ks=(1,),
    )
    assert out["cases"] == 2
    assert out["cases_missing_results"] == ["c2"]
    assert out["aggregate"]["recall@1"] == 0.5


def test_deferred_when_inputs_missing(tmp_path) -> None:
    out_file = tmp_path / "visual_eval.json"
    report = run_visual_eval(
        kb_path=None,
        results_path=str(tmp_path / "nope.jsonl"),
        gold_path=str(tmp_path / "nope2.jsonl"),
        out_path=str(out_file),
    )
    assert report["status"] == "deferred"
    assert report["metrics"] is None
    assert "deferred" in report["reason"]
    on_disk = json.loads(out_file.read_text(encoding="utf-8"))
    assert on_disk["status"] == "deferred"
    assert on_disk["metrics"] is None


def test_real_run_with_synthetic_inputs(tmp_path) -> None:
    case = next(c for c in _eval_fixtures() if c["case_id"] == "VE-EVAL01")
    kb = tmp_path / "02_visuals_kb.jsonl"
    kb.write_text('{"figure_id": "abc123def4567890:p3:f0"}\n', encoding="utf-8")
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({
        "case_id": case["case_id"], "ranked_candidates": case["ranked_candidates"],
    }) + "\n", encoding="utf-8")
    gold = tmp_path / "gold.jsonl"
    gold.write_text(json.dumps({
        "case_id": case["case_id"], "gold_figure_ids": case["gold_figure_ids"],
    }) + "\n", encoding="utf-8")
    report = run_visual_eval(
        kb_path=str(kb), results_path=str(results), gold_path=str(gold), ks=(1, 3)
    )
    assert report["status"] == "ok"
    assert report["metrics"]["per_case"][case["case_id"]]["recall@1"] == 0.0
    assert report["metrics"]["per_case"][case["case_id"]]["recall@3"] == 1.0
