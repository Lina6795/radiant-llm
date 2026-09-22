"""Runner integration: schema stability, skip semantics, baseline compare."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.eval.release_gate import evaluate_gate
from app.eval.runner import SCHEMA_VERSION, render_markdown, run_layers

FAST_LAYERS = ["control", "context", "verification"]


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    # Deterministic skip for answer_cases: no DeepSeek key in test env.
    os.environ.pop("OPENAI_API_KEY", None)
    out_dir = tmp_path_factory.mktemp("eval-run") / "run-a"
    out_dir.mkdir()
    return run_layers(FAST_LAYERS, out_dir)


class TestReportSchema:
    def test_top_level_schema_stable(self, report):
        assert report["schema_version"] == SCHEMA_VERSION
        for key in ("run_id", "created_at", "config_fingerprint", "discovery",
                    "layers", "metrics_flat", "summary", "baseline_comparison"):
            assert key in report, key

    def test_layer_schema_stable(self, report):
        for layer in FAST_LAYERS:
            lr = report["layers"][layer]
            for key in ("status", "skip_reason", "datasets", "cases",
                        "metrics", "duration_s"):
                assert key in lr, (layer, key)

    def test_every_case_traces_back(self, report):
        """Each case carries case_id + config fingerprint + artifact path."""
        for layer, lr in report["layers"].items():
            for case in lr["cases"]:
                assert case["case_id"]
                assert case["config_fingerprint"]
                assert case["config_fingerprint"] == \
                    report["config_fingerprint"]["fingerprint_hash"]
                assert case["artifact_uri"] or case["trace_uri"], case["case_id"]
                assert case["dataset"]
                assert case["status"] in ("pass", "fail", "skip")

    def test_metric_entries_typed(self, report):
        for lr in report["layers"].values():
            for name, metric in lr["metrics"].items():
                assert metric["status"] in ("measured", "not_measured")
                assert metric["kind"] in ("deterministic", "judge",
                                          "human_audit", "operational")

    def test_control_layer_all_pass(self, report):
        cases = report["layers"]["control"]["cases"]
        assert len(cases) == 28  # 12 router + 16 policy
        assert all(c["status"] == "pass" for c in cases)
        assert report["metrics_flat"]["control.pass_rate"] == 1.0

    def test_dataset_data_version_recorded(self, report):
        ds = report["layers"]["control"]["datasets"]["router_cases"]
        assert ds["data_version_hash"]
        assert ds["n_cases"] == 12


class TestSkipSemantics:
    def test_answer_dataset_skips_without_key_visual_runs(self, report):
        lr = report["layers"]["verification"]
        # visual fixtures always run; answer_cases skips without OPENAI_API_KEY
        assert lr["status"] == "ok"
        answer = lr["datasets"]["answer_cases"]
        assert answer["status"] == "skipped"
        assert "OPENAI_API_KEY" in answer["skip_reason"]
        visual_cases = [c for c in lr["cases"] if c["dataset"] == "visual_cases"]
        assert len(visual_cases) == 11
        assert all(c["status"] == "pass" for c in visual_cases)
        # a skipped dataset contributes no fake metrics
        assert "verification.hr" not in report["metrics_flat"]

    def test_missing_datasets_recorded_in_discovery(self, report):
        assert isinstance(report["discovery"]["missing_datasets"], list)

    def test_skipped_dataset_not_an_error(self, report):
        assert report["summary"]["layers_failed"] == 0


class TestOutputFiles:
    def test_report_files_written(self, report, tmp_path):
        out_dir = tmp_path / "run-b"
        out_dir.mkdir()
        from app.eval.runner import main

        rc = main(["--layer", "control", "--out", str(out_dir)])
        assert rc == 0
        written = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        assert written["schema_version"] == SCHEMA_VERSION
        md = (out_dir / "report.md").read_text(encoding="utf-8")
        assert "# Eval report" in md
        assert "## control" in md

    def test_markdown_rendering(self, report):
        md = render_markdown(report)
        assert "## control" in md
        assert "## verification" in md
        assert "skipped" in md


class TestBaselineComparison:
    def test_self_comparison_zero_deltas(self, tmp_path, report):
        out_a = tmp_path / "run-a"
        out_a.mkdir(exist_ok=True)
        (out_a / "report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
        out_b = tmp_path / "run-b"
        out_b.mkdir()
        current = run_layers(FAST_LAYERS, out_b, baseline_path=out_a / "report.json")
        comparison = current["baseline_comparison"]
        assert comparison["baseline_run_id"] == report["run_id"]
        for key, delta in comparison["metrics"].items():
            if delta["delta"] is not None:
                assert delta["delta"] == pytest.approx(0.0), key

    def test_gate_passes_self_vs_self(self, tmp_path, report):
        """Full closure: a real report passes the gate against itself
        only when integrity layers are green -- control+context alone
        cannot satisfy the durable/memory integrity rules, so assert the
        fail-closed reason instead."""
        result = evaluate_gate(report, report)
        failed = [r for r in result["rules"] if r["status"] == "fail"]
        # durable and memory are absent here -> integrity must fail closed
        assert any("durable" in r["detail"] for r in failed)
        assert any("memory" in r["detail"] for r in failed)
