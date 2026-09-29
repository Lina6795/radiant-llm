"""S11-A: the release gate must refuse regression comparisons between
semantically incompatible reports (baseline_incompatible, fail-closed)
instead of emitting ordinary regression verdicts.
"""

from __future__ import annotations

import copy

from app.eval.release_gate import evaluate_gate

from tests.eval.test_release_gate import _failed_rules, _report


def _rules(result: dict, name: str) -> list[dict]:
    return [r for r in result["rules"] if r["rule"] == name]


class TestBaselineCompatibility:
    def test_compatible_pair_reports_compatibility_pass(self):
        result = evaluate_gate(_report("b"), _report("a"))
        compat = _rules(result, "baseline_compatibility")
        assert len(compat) == 1 and compat[0]["status"] == "pass"
        assert result["gate"] == "pass"

    def test_legacy_report_without_metadata_is_incompatible(self):
        baseline = _report("a")
        for key in ("metric_schema_version", "evaluator_version",
                    "dataset_digest", "random_seed", "config_fingerprint"):
            baseline.pop(key)
        result = evaluate_gate(_report("b"), baseline)
        assert result["gate"] == "fail"
        compat = _rules(result, "baseline_compatibility")
        assert len(compat) == 1 and compat[0]["status"] == "fail"
        assert "baseline_incompatible" in compat[0]["detail"]
        assert "metric_schema_version" in compat[0]["detail"]

    def test_regression_rules_suppressed_when_incompatible(self):
        baseline = _report("a")
        baseline.pop("config_fingerprint")
        result = evaluate_gate(_report("b"), baseline)
        for name in ("core_recall", "unsupported_rate", "layer_status"):
            suppressed = _rules(result, name)
            assert suppressed and all(r["status"] == "skip" for r in suppressed), name
            assert any("baseline_incompatible" in r["detail"] for r in suppressed)

    def test_integrity_still_runs_when_incompatible(self):
        """Candidate-only safety rules are meaningful even when the pair
        cannot be compared for regression."""
        baseline = _report("a")
        baseline.pop("config_fingerprint")
        current = _report("b")
        current["layers"]["durable"]["cases"][0]["status"] = "fail"
        result = evaluate_gate(current, baseline)
        failed = _failed_rules(result)
        assert any(r["rule"] == "integrity" for r in failed)

    def test_dataset_digest_mismatch_named_in_detail(self):
        baseline = _report("a")
        baseline["dataset_digest"] = "other-digest"
        result = evaluate_gate(_report("b"), baseline)
        compat = _rules(result, "baseline_compatibility")[0]
        assert compat["status"] == "fail"
        assert "dataset_digest" in compat["detail"]

    def test_corpus_digest_mismatch_is_incompatible(self):
        """The S10 red light: same benchmark files but re-ingested evidence
        corpus must not be compared as a regression."""
        baseline = _report("a")
        baseline["config_fingerprint"]["evidence_store"]["content_digest"] = "v1-corpus"
        result = evaluate_gate(_report("b"), baseline)
        compat = _rules(result, "baseline_compatibility")[0]
        assert compat["status"] == "fail"
        assert "evidence_store.content_digest" in compat["detail"]

    def test_both_storeless_is_compatible(self):
        baseline, current = _report("a"), _report("b")
        for report in (baseline, current):
            report["config_fingerprint"]["evidence_store"] = {"content_digest": None}
            report["config_fingerprint"]["vector_store"] = {"content_digest": None}
        result = evaluate_gate(current, baseline)
        assert _rules(result, "baseline_compatibility")[0]["status"] == "pass"

    def test_one_side_storeless_is_incompatible(self):
        baseline = _report("a")
        baseline["config_fingerprint"]["evidence_store"] = {"content_digest": None}
        result = evaluate_gate(_report("b"), baseline)
        assert _rules(result, "baseline_compatibility")[0]["status"] == "fail"

    def test_layer_evaluator_version_change_is_incompatible(self):
        """A verifier/metric code change (e.g. claims.py split rules) bumps
        the layer evaluator version and blocks naive comparison."""
        baseline = _report("a")
        baseline["config_fingerprint"]["layer_evaluator_versions"]["retrieval"] = \
            "retrieval-exec/v1"
        result = evaluate_gate(_report("b"), baseline)
        compat = _rules(result, "baseline_compatibility")[0]
        assert compat["status"] == "fail"
        assert "layer_evaluator_versions.retrieval" in compat["detail"]


class TestFingerprintMetadata:
    def test_fingerprint_carries_comparability_fields(self):
        from app.eval.fingerprint import collect_fingerprint

        fp = collect_fingerprint()
        assert fp["metric_schema_version"]
        assert fp["evaluator_version"]
        assert fp["layer_evaluator_versions"]["retrieval"]
        assert fp["dataset_digest"]  # benchmarks/ exists in the repo
        assert fp["random_seed"] is not None
        for section in ("retrieval", "verification"):
            assert fp[section]["config_fingerprint"], section
        for store in ("evidence_store", "vector_store"):
            assert "content_digest" in fp[store]

    def test_dataset_digest_stable_and_sensitive(self):
        from app.eval.fingerprint import _dataset_digest

        versions = {"a.jsonl": {"sha256": "x"}, "b.jsonl": {"sha256": "y"}}
        assert _dataset_digest(versions) == _dataset_digest(
            {"b.jsonl": {"sha256": "y"}, "a.jsonl": {"sha256": "x"}})
        assert _dataset_digest(versions) != _dataset_digest(
            {"a.jsonl": {"sha256": "x"}, "b.jsonl": {"sha256": "z"}})
        assert _dataset_digest({}) is None
