"""Verification layer adapter: skip semantics + real offline execution.

* visual_cases always runs against the frozen synthetic fixtures (real
  M7 ``visual_qa`` / ``visual_eval`` math);
* answer_cases skips without OPENAI_API_KEY (recorded, never faked) and
  executes the real M7 B2 arm (planner -> durable runner -> BM25 ->
  rule verifier -> resume) when a draft endpoint is available -- in
  tests the DeepSeek call is stubbed, everything else runs for real.
"""

from __future__ import annotations

import pytest

from app.eval.adapters import LayerContext, run_verification_layer
from app.eval.registry import get_spec, load_cases


@pytest.fixture()
def ctx(tmp_path):
    return LayerContext(run_id="test-verification", out_dir=tmp_path,
                        fingerprint_hash="fp-test")


class TestVisualCases:
    def test_real_fixtures_execute_and_pass(self, ctx):
        spec = get_spec("visual_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["visual_cases"]
        assert ds.status == "ok"
        assert len(ds.cases) == 11
        assert all(c.status == "pass" for c in ds.cases), [
            (c.case_id, c.error) for c in ds.cases if c.status != "pass"]
        assert ds.metrics["pass_rate"].value == 1.0
        # ViR family metric is measured from the eval fixtures.
        assert ds.metrics["vir"].status == "measured"
        assert ds.metrics["vir"].value == pytest.approx(2 / 3)

    def test_fixture_semantics_match_m7(self, ctx):
        spec = get_spec("visual_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["visual_cases"]
        by_id = {c.case_id: c for c in ds.cases}
        assert by_id["VQ-OK01"].metrics["got_issues"] == []
        assert set(by_id["VQ-EMPTY01"].metrics["got_issues"]) >= {
            "empty_description", "low_information"}
        assert "wrong_page" in by_id["VQ-PAGE01"].metrics["got_issues"]
        # eval fixture recall math reproduced the frozen expectations
        checks = by_id["VE-EVAL01"].metrics["checks"]
        assert checks["expect_recall_at_1"]["got"] == 0.0
        assert checks["expect_recall_at_3"]["got"] == 1.0

    def test_every_case_traceable(self, ctx):
        spec = get_spec("visual_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["visual_cases"]
        for case in ds.cases:
            assert case.config_fingerprint == "fp-test"
            assert case.artifact_uri
            assert case.latency_ms is not None


class TestAnswerSkipSemantics:
    def test_skips_without_api_key(self, ctx, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        spec = get_spec("answer_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["answer_cases"]
        assert ds.status == "skipped"
        assert "OPENAI_API_KEY" in ds.skip_reason
        assert ds.cases == []           # no fabricated case results
        assert ds.metrics == {}         # no fabricated metrics


class TestAnswerB2Arm:
    """Real B2 mechanics with only the DeepSeek draft call stubbed."""

    @pytest.fixture()
    def stubbed_llm(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        from app.verification import experiment as m7

        def stub(messages, **kwargs):
            user = messages[-1]["content"]
            snippet = user.split("EVIDENCE:\n", 1)[1].split("\n\nQUESTION", 1)[0]
            text = " ".join(snippet.split()[:25])
            return {"content": f"According to the evidence, {text} [p1].",
                    "latency_ms": 1.0, "prompt_tokens": 10,
                    "completion_tokens": 10}

        monkeypatch.setattr(m7, "deepseek_chat", stub)
        return m7

    def test_b2_arm_real_execution(self, ctx, stubbed_llm):
        spec = get_spec("answer_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["answer_cases"]
        assert ds.status == "ok"
        assert len(ds.cases) == 12
        assert all(c.status == "pass" for c in ds.cases), [
            (c.case_id, c.error) for c in ds.cases if c.status != "pass"]

    def test_b2_metrics_measured(self, ctx, stubbed_llm):
        spec = get_spec("answer_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["answer_cases"]
        for name in ("claim_support_rate", "citation_precision",
                     "citation_coverage", "unsupported_rate", "hr",
                     "fact_recall", "escalation_precision"):
            assert ds.metrics[name].status == "measured", name
        # HR is the unsupported-rate alias the release gate watches.
        assert ds.metrics["hr"].value == ds.metrics["unsupported_rate"].value
        assert 0.0 <= ds.metrics["hr"].value <= 1.0

    def test_b2_cases_traceable_to_run_and_verdict(self, ctx, stubbed_llm):
        spec = get_spec("answer_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["answer_cases"]
        for case in ds.cases:
            assert case.config_fingerprint == "fp-test"
            assert case.trace_uri
            assert case.metrics["answer_model"]
            assert case.metrics["verification_action"] in (
                "commit", "retrieve_more", "human_review", "abstain")
            assert case.metrics["claims"] and case.metrics["claims"] >= 1

    def test_refusal_cases_measured(self, ctx, stubbed_llm):
        spec = get_spec("answer_cases")
        ds = run_verification_layer({spec: load_cases(spec)}, ctx)["answer_cases"]
        metric = ds.metrics["refusal_cases_correct"]
        assert metric.status == "measured"
        assert metric.n_cases == 2      # two frozen refusal cases
