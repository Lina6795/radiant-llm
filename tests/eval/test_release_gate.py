"""Release gate: pass path (report vs itself) and every reject path."""

from __future__ import annotations

import copy

import pytest

from app.eval.release_gate import GateConfig, evaluate_gate


def _case(case_id: str, status: str = "pass") -> dict:
    return {"case_id": case_id, "dataset": "ds", "status": status,
            "metrics": {}, "error": None, "artifact_uri": None,
            "trace_uri": None, "latency_ms": 1.0, "config_fingerprint": "fp"}


def _compat_block() -> dict:
    """The S11-A comparability metadata a v2 report must carry."""
    return {
        "metric_schema_version": "radiant-eval-metrics/v2",
        "evaluator_version": "eval-harness/v2",
        "dataset_digest": "ds-digest-1",
        "random_seed": 0,
        "config_fingerprint": {
            "layer_evaluator_versions": {
                "control": "control-exec/v1", "durable": "durable-exec/v1",
                "retrieval": "retrieval-exec/v2", "context": "context-exec/v1",
                "memory": "memory-exec/v1"},
            "retrieval": {"config_fingerprint": "ret-fp-1"},
            "verification": {"config_fingerprint": "ver-fp-1"},
            "evidence_store": {"content_digest": "ev-digest-1"},
            "vector_store": {"content_digest": "vs-digest-1"},
        },
    }


def _report(run_id: str = "run") -> dict:
    """A minimal but complete report that must pass the gate vs itself."""
    report = {
        "schema_version": "radiant-eval-report/v2",
        "run_id": run_id,
        "metrics_flat": {
            "retrieval.recall@20": 0.9,
            "retrieval.anchor_hit@20": 1.0,
            "retrieval.recall@5": 0.8,
        },
        "layers": {
            "control": {"status": "ok", "cases": [_case("RT-01")], "metrics": {}},
            "durable": {"status": "ok",
                        "cases": [_case(f"RT-0{i}") for i in range(1, 9)],
                        "metrics": {}},
            "retrieval": {"status": "ok", "cases": [_case("RET-T01")], "metrics": {}},
            "context": {"status": "ok",
                        "cases": [_case("CTX-T05"), _case("CTX-T07"),
                                  _case("CTX-T08"), _case("CTX-T01")],
                        "metrics": {}},
            "memory": {"status": "ok", "cases": [_case("MEM-01")], "metrics": {}},
        },
    }
    report.update(_compat_block())
    return report


def _failed_rules(result: dict) -> list[dict]:
    return [r for r in result["rules"] if r["status"] == "fail"]


class TestPassPath:
    def test_same_report_vs_itself_passes(self):
        result = evaluate_gate(_report("a"), _report("a"))
        assert result["gate"] == "pass", _failed_rules(result)
        assert result["n_fail"] == 0

    def test_improvement_passes(self):
        current = _report("b")
        current["metrics_flat"]["retrieval.recall@20"] = 0.95
        assert evaluate_gate(current, _report("a"))["gate"] == "pass"

    def test_configurable_tolerance(self):
        baseline, current = _report("a"), _report("b")
        current["metrics_flat"]["retrieval.recall@20"] = 0.85  # drop 0.05
        assert evaluate_gate(current, baseline)["gate"] == "fail"
        config = GateConfig(recall_max_drop=0.1)
        assert evaluate_gate(current, baseline, config)["gate"] == "pass"


class TestRecallRejects:
    def test_recall_regression_fails(self):
        current = _report("b")
        current["metrics_flat"]["retrieval.recall@20"] = 0.7
        result = evaluate_gate(current, _report("a"))
        assert result["gate"] == "fail"
        failed = _failed_rules(result)
        assert any(r["rule"] == "core_recall"
                   and r["metric"] == "retrieval.recall@20" for r in failed)

    def test_recall_missing_in_current_fails_closed(self):
        current = _report("b")
        del current["metrics_flat"]["retrieval.recall@20"]
        result = evaluate_gate(current, _report("a"))
        assert result["gate"] == "fail"


class TestUnsupportedRejects:
    def test_unsupported_rise_fails(self):
        baseline, current = _report("a"), _report("b")
        baseline["metrics_flat"]["verification.hr"] = 0.1
        current["metrics_flat"]["verification.hr"] = 0.2
        result = evaluate_gate(current, baseline)
        assert result["gate"] == "fail"
        assert any(r["rule"] == "unsupported_rate" for r in _failed_rules(result))

    def test_unsupported_unmeasured_in_current_fails_closed(self):
        baseline = _report("a")
        baseline["metrics_flat"]["verification.hr"] = 0.1
        result = evaluate_gate(_report("b"), baseline)
        assert result["gate"] == "fail"

    def test_unsupported_unmeasured_both_sides_skips(self):
        result = evaluate_gate(_report("b"), _report("a"))
        skips = [r for r in result["rules"]
                 if r["rule"] == "unsupported_rate" and r["status"] == "skip"]
        assert skips  # never measured -> rule skipped, gate still passes
        assert result["gate"] == "pass"


class TestIntegrityRejects:
    def test_durable_case_failure_fails(self):
        current = _report("b")
        current["layers"]["durable"]["cases"][3]["status"] = "fail"
        assert evaluate_gate(current, _report("a"))["gate"] == "fail"

    def test_memory_case_skip_fails(self):
        current = _report("b")
        current["layers"]["memory"]["cases"][0]["status"] = "skip"
        assert evaluate_gate(current, _report("a"))["gate"] == "fail"

    def test_context_isolation_case_failure_fails(self):
        current = _report("b")
        current["layers"]["context"]["cases"][1]["status"] = "fail"  # CTX-T07
        assert evaluate_gate(current, _report("a"))["gate"] == "fail"

    def test_skipped_integrity_layer_fails_closed(self):
        current = _report("b")
        current["layers"]["durable"] = {"status": "skipped", "cases": [],
                                        "metrics": {}}
        assert evaluate_gate(current, _report("a"))["gate"] == "fail"


class TestJudgeAuditRejects:
    def _add_judge_metric(self, report: dict, human_review) -> None:
        report["layers"]["verification"] = {
            "status": "ok", "cases": [],
            "metrics": {
                "cop": {"name": "cop", "value": 0.7, "status": "measured",
                        "kind": "judge", "n_cases": 10, "reason": None,
                        "judge": {"judge_model": "deepseek-v4-pro",
                                  "prompt_template_hash": "abc", "n_repeats": 2},
                        "human_review": human_review},
            },
        }

    def test_judge_metric_without_human_review_fails(self):
        current = _report("b")
        self._add_judge_metric(current, None)
        result = evaluate_gate(current, _report("a"))
        assert result["gate"] == "fail"
        assert any(r["rule"] == "judge_human_audit" for r in _failed_rules(result))

    def test_judge_metric_with_human_review_passes(self):
        current = _report("b")
        self._add_judge_metric(current, {"agreement": 0.85, "n_audited": 20})
        assert evaluate_gate(current, _report("a"))["gate"] == "pass"

    def test_not_measured_judge_metric_is_fine(self):
        current = _report("b")
        current["layers"]["verification"] = {
            "status": "skipped", "cases": [],
            "metrics": {"cop": {"name": "cop", "value": None,
                                "status": "not_measured", "kind": "judge",
                                "n_cases": 0, "reason": "no_judge",
                                "judge": None, "human_review": None}},
        }
        assert evaluate_gate(current, _report("a"))["gate"] == "pass"


class TestLayerStatusRejects:
    def test_newly_failed_layer_fails(self):
        current = _report("b")
        current["layers"]["retrieval"]["status"] = "failed"
        assert evaluate_gate(current, _report("a"))["gate"] == "fail"


class TestConfig:
    def test_unknown_config_key_rejected(self):
        with pytest.raises(ValueError):
            GateConfig.from_dict({"nonsense": 1})

    def test_from_dict_round_trip(self):
        config = GateConfig.from_dict({"recall_max_drop": 0.05,
                                       "recall_metrics": ["a.b"]})
        assert config.recall_max_drop == 0.05
        assert config.recall_metrics == ["a.b"]
