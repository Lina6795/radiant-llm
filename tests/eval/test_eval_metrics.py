"""Hand-computed correctness checks for the five paper metrics."""

from __future__ import annotations

import pytest

from app.eval.metrics import (
    DEFAULT_ALPHA,
    SEMANTIC_GRADES,
    cih,
    cip,
    cop,
    hr,
    numeric_fidelity,
    vir,
)


class TestCoP:
    def test_semantic_only_returns_grade(self):
        for grade in SEMANTIC_GRADES:
            assert cop(grade) == grade

    def test_rejects_non_discrete_grade(self):
        with pytest.raises(ValueError):
            cop(0.3)
        with pytest.raises(ValueError):
            cop(1.1)

    def test_numeric_blend_hand_computed(self):
        # semantic 0.75, v=9 vs v*=10 -> fidelity 1 - 1/10 = 0.9
        # alpha 0.6: 0.6*0.75 + 0.4*0.9 = 0.45 + 0.36 = 0.81
        assert cop(0.75, 9.0, 10.0) == pytest.approx(0.81)

    def test_numeric_blend_custom_alpha(self):
        # alpha 0.5: 0.5*1.0 + 0.5*0.9 = 0.95
        assert cop(1.0, 9.0, 10.0, alpha=0.5) == pytest.approx(0.95)

    def test_numeric_exact_match(self):
        assert cop(0.5, 42.0, 42.0) == pytest.approx(0.5 * 0.6 + 0.4 * 1.0)

    def test_fidelity_clipped_at_zero(self):
        assert numeric_fidelity(100.0, 10.0) == 0.0
        assert cop(0.0, 100.0, 10.0) == pytest.approx(0.0)

    def test_gold_zero_uses_epsilon(self):
        # |v*|=0 -> denominator max(0, eps); v=0 -> fidelity 1
        assert numeric_fidelity(0.0, 0.0) == pytest.approx(1.0)
        assert numeric_fidelity(1.0, 0.0) == 0.0

    def test_alpha_validated(self):
        with pytest.raises(ValueError):
            cop(1.0, 1.0, 1.0, alpha=1.5)


class TestCiP:
    def test_hand_computed(self):
        assert cip([True, True, True, False]) == pytest.approx(0.75)

    def test_all_effective(self):
        assert cip([True, True]) == 1.0

    def test_no_citations_is_not_measured(self):
        assert cip([]) is None


class TestCiH:
    def test_hand_computed(self):
        assert cih([True, False, True, True]) == pytest.approx(0.75)

    def test_single_binary(self):
        assert cih([True]) == 1.0
        assert cih([False]) == 0.0

    def test_empty_is_not_measured(self):
        assert cih([]) is None


class TestHR:
    def test_hand_computed(self):
        # 2 unsupported of 5 claims
        assert hr([True, False, True, False, True]) == pytest.approx(0.4)

    def test_zero_when_all_supported(self):
        assert hr([True, True]) == 0.0

    def test_no_claims_is_not_measured(self):
        assert hr([]) is None


class TestViR:
    def test_hand_computed(self):
        gold = ["bar_chart_axis", "table_cell_3", "figure_caption", "diagram_arrow"]
        recalled = ["bar_chart_axis", "figure_caption", "noise"]
        assert vir(gold, recalled) == pytest.approx(0.5)

    def test_full_recall(self):
        assert vir(["a", "b"], ["b", "a", "extra"]) == 1.0

    def test_no_gold_facts_is_not_measured(self):
        assert vir([], ["anything"]) is None
