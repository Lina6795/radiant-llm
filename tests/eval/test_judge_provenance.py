"""Judge provenance enforcement.

Red lines under test: every judged score records judge model, prompt
template hash and repeat count; without a judge the metric is
``not_measured`` -- never a default perfect score.
"""

from __future__ import annotations

import pytest

from app.eval.metrics import (
    hash_prompt_template,
    judged_metric,
    manual_scores,
    run_judge,
)

TEMPLATE = "Rate the support of this claim in [0,1]:\n{item}"


def _stub_judge(prompt: str) -> float:
    assert prompt.startswith("Rate the support")
    return 0.5


class TestRunJudge:
    def test_provenance_recorded(self):
        scores, prov = run_judge(
            _stub_judge, model="deepseek-v4-pro", prompt_template=TEMPLATE,
            items=["claim a", "claim b"], n_repeats=3)
        assert scores == [0.5, 0.5]
        assert prov.judge_model == "deepseek-v4-pro"
        assert prov.judge_kind == "model"
        assert prov.prompt_template_hash == hash_prompt_template(TEMPLATE)
        assert prov.n_repeats == 3
        assert prov.raw_scores == [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]]
        assert prov.created_at

    def test_prompt_template_hash_matches_template(self):
        _, prov = run_judge(_stub_judge, model="m", prompt_template=TEMPLATE,
                            items=["x"])
        import hashlib

        assert prov.prompt_template_hash == hashlib.sha256(
            TEMPLATE.encode("utf-8")).hexdigest()

    def test_repeat_mean(self):
        answers = iter([0.0, 1.0])
        scores, prov = run_judge(lambda _p: next(answers), model="m",
                                 prompt_template=TEMPLATE, items=["x"],
                                 n_repeats=2)
        assert scores == [0.5]
        assert prov.raw_scores == [[0.0, 1.0]]

    def test_model_id_required(self):
        with pytest.raises(ValueError):
            run_judge(_stub_judge, model="", prompt_template=TEMPLATE, items=["x"])

    def test_template_must_have_placeholder(self):
        with pytest.raises(ValueError):
            run_judge(_stub_judge, model="m", prompt_template="no placeholder",
                      items=["x"])

    def test_repeats_at_least_one(self):
        with pytest.raises(ValueError):
            run_judge(_stub_judge, model="m", prompt_template=TEMPLATE,
                      items=["x"], n_repeats=0)

    def test_score_range_enforced(self):
        with pytest.raises(ValueError):
            run_judge(lambda _p: 1.5, model="m", prompt_template=TEMPLATE,
                      items=["x"])


class TestJudgedMetric:
    def test_no_judge_is_not_measured_never_full_score(self):
        result = judged_metric("cop", None, items=["a", "b"])
        assert result.status == "not_measured"
        assert result.value is None
        assert "no_judge" in result.reason
        assert result.judge is None

    def test_measured_carries_provenance(self):
        result = judged_metric("cop", _stub_judge, model="deepseek-v4-pro",
                               prompt_template=TEMPLATE, items=["a", "b"],
                               n_repeats=2,
                               human_review={"agreement": 0.9, "n_audited": 20})
        assert result.status == "measured"
        assert result.value == pytest.approx(0.5)
        assert result.kind == "judge"
        assert result.judge["judge_model"] == "deepseek-v4-pro"
        assert result.judge["n_repeats"] == 2
        assert result.judge["prompt_template_hash"] == hash_prompt_template(TEMPLATE)
        assert result.human_review["agreement"] == 0.9


class TestManualScores:
    def test_human_entry_point(self):
        scores, prov = manual_scores([0.0, 0.25, 1.0], rater="reviewer-1",
                                     rubric="0=wrong, 0.25=poor, ...")
        assert scores == [0.0, 0.25, 1.0]
        assert prov.judge_kind == "human"
        assert prov.judge_model == "human:reviewer-1"
        assert prov.prompt_template_hash == hash_prompt_template(
            "0=wrong, 0.25=poor, ...")

    def test_rater_required(self):
        with pytest.raises(ValueError):
            manual_scores([1.0], rater="", rubric="r")

    def test_score_range_enforced(self):
        with pytest.raises(ValueError):
            manual_scores([2.0], rater="r1", rubric="r")
