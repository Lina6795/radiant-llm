"""S11-C: detected (draft risk) vs final-committed (answer quality) metric
separation, and the gate's switch to final_committed_unsupported_rate.
"""

from __future__ import annotations

from app.eval.adapters import _aggregate_answer_metrics
from app.eval.release_gate import evaluate_gate

from tests.eval.test_release_gate import _failed_rules, _report


def _rows():
    cases = [{"category": "normal"} for _ in range(6)]
    rows = [
        # accept, fully supported
        {"verification_action": "commit", "committed": True,
         "unsupported_rate": 0.0, "support_rate": 1.0,
         "detected_unsupported_rate": 0.0,
         "escalated": False, "gold_escalate": False, "answer": "a"},
        # retrieve_more (revise path) -- committed, partially supported
        {"verification_action": "retrieve_more", "committed": True,
         "unsupported_rate": 0.5, "support_rate": 0.5,
         "detected_unsupported_rate": 0.5,
         "escalated": False, "gold_escalate": True, "answer": "b"},
        # human review -- NOT committed, fully unsupported
        {"verification_action": "human_review", "committed": False,
         "unsupported_rate": 1.0, "support_rate": 0.0,
         "detected_unsupported_rate": 1.0,
         "escalated": True, "gold_escalate": True, "answer": "c"},
        # abstain -- empty claims, rates None (must be skipped in means)
        {"verification_action": "abstain", "committed": False,
         "unsupported_rate": None, "support_rate": None,
         "detected_unsupported_rate": None,
         "escalated": False, "gold_escalate": False,
         "answer": None, "refused": True},
        # clarify -- NOT committed; detected != committed-arm re-verify
        {"verification_action": "clarify", "committed": False,
         "unsupported_rate": 0.25, "support_rate": 0.75,
         "detected_unsupported_rate": 0.75,
         "escalated": False, "gold_escalate": False, "answer": "e"},
        # error row -- never reached a decision
        {"verification_action": None, "committed": False,
         "unsupported_rate": None, "support_rate": None,
         "detected_unsupported_rate": None,
         "escalated": False, "gold_escalate": False,
         "answer": None, "error": "boom"},
    ]
    return cases, rows


class TestDetectedVsCommitted:
    def test_committed_metric_excludes_review_and_abstain(self):
        cases, rows = _rows()
        m = _aggregate_answer_metrics(rows, cases)
        # committed rows: commit (0.0) + retrieve_more (0.5) -> 0.25
        assert m["final_committed_unsupported_rate"].value == 0.25
        assert m["final_committed_unsupported_rate"].status == "measured"

    def test_detected_uses_gate_time_values(self):
        cases, rows = _rows()
        m = _aggregate_answer_metrics(rows, cases)
        # detected: [0.0, 0.5, 1.0, None, 0.75, None] -> 2.25/4
        assert m["detected_unsupported_rate"].value == 0.5625
        # legacy hr unchanged: unsupported [0.0, 0.5, 1.0, None, 0.25, None]
        assert m["hr"].value == 0.4375
        assert m["unsupported_rate"].value == m["hr"].value

    def test_outcome_rates_over_decided_cases(self):
        cases, rows = _rows()
        m = _aggregate_answer_metrics(rows, cases)
        # decided: 5 rows with an action; review 1/5, accept 1/5
        assert m["review_rate"].value == 0.2
        assert m["accept_rate"].value == 0.2
        assert m["review_rate"].n_cases == 5

    def test_supported_claim_rate_alias_measured(self):
        cases, rows = _rows()
        m = _aggregate_answer_metrics(rows, cases)
        # support: [1.0, 0.5, 0.0, None, 0.75, None] -> 2.25/4
        assert m["supported_claim_rate"].value == 0.5625
        assert m["claim_support_rate"].value == m["supported_claim_rate"].value

    def test_no_committed_rows_is_not_measured(self):
        cases = [{"category": "normal"}]
        rows = [{"verification_action": "human_review", "committed": False,
                 "unsupported_rate": 1.0, "support_rate": 0.0,
                 "detected_unsupported_rate": 1.0,
                 "escalated": True, "gold_escalate": True, "answer": "c"}]
        m = _aggregate_answer_metrics(rows, cases)
        assert m["final_committed_unsupported_rate"].status == "not_measured"
        assert m["review_rate"].value == 1.0


def _with_committed_metric(report: dict, value: float | None) -> dict:
    if value is None:
        report["metrics_flat"].pop("verification.final_committed_unsupported_rate", None)
    else:
        report["metrics_flat"]["verification.final_committed_unsupported_rate"] = value
    return report


class TestGateUsesCommittedRate:
    def test_hr_rise_does_not_fail_gate(self):
        """The legacy draft-level hr may drift with verifier strictness and
        LLM sampling; the gate must not watch it anymore."""
        baseline = _with_committed_metric(_report("a"), 0.2)
        baseline["metrics_flat"]["verification.hr"] = 0.3
        current = _with_committed_metric(_report("b"), 0.2)
        current["metrics_flat"]["verification.hr"] = 0.9  # big hr rise
        result = evaluate_gate(current, baseline)
        assert result["gate"] == "pass", _failed_rules(result)

    def test_committed_rise_fails(self):
        baseline = _with_committed_metric(_report("a"), 0.2)
        current = _with_committed_metric(_report("b"), 0.4)
        result = evaluate_gate(current, baseline)
        assert result["gate"] == "fail"
        assert any(r["rule"] == "unsupported_rate"
                   and r["metric"] == "verification.final_committed_unsupported_rate"
                   for r in _failed_rules(result))

    def test_committed_unmeasured_in_current_fails_closed(self):
        baseline = _with_committed_metric(_report("a"), 0.2)
        current = _with_committed_metric(_report("b"), None)
        result = evaluate_gate(current, baseline)
        assert result["gate"] == "fail"
        assert any(r["rule"] == "unsupported_rate" for r in _failed_rules(result))
