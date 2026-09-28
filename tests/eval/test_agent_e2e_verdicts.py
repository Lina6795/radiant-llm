"""S10 regression: the agent_e2e layer must not count verify_action=="review"
as pass, and every executed case must carry trace_uri + config_fingerprint +
latency so aggregates trace back to case + configuration + trace.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.eval import agent_e2e
from app.eval.adapters import LayerContext


class _FakeEvents:
    def stream(self, run_id: str):
        return [
            SimpleNamespace(seq=1, type=SimpleNamespace(value="RUN_STARTED"),
                            payload={"run_id": run_id}, created_at=0.0),
        ]


class _Spec:
    name = "fake_ds"


def _fake_runtime(run_status: str, verify_action: str | None, claims: list):
    verify_output = {
        "verify_action": verify_action,
        "claims": claims,
        "answer": "The Transformer consists of an encoder and a decoder.",
        "revise_used": False,
    }
    results = {}
    if verify_action is not None:
        results["s5-verify"] = SimpleNamespace(
            output=verify_output, error=None, status=SimpleNamespace(value="success"))
    report = SimpleNamespace(
        status=SimpleNamespace(value=run_status),
        results=results,
    )
    return {
        "router": lambda goal: SimpleNamespace(action=SimpleNamespace(value="tool_call")),
        "planner": lambda decision, goal: {"fake": "plan"},
        "guard": SimpleNamespace(validate=lambda plan: SimpleNamespace(ok=True)),
        "policy": SimpleNamespace(
            authorize=lambda plan, ws: SimpleNamespace(verdict=SimpleNamespace(value="allow"))),
        "runner": SimpleNamespace(run=lambda plan, workspace: report),
        "events": _FakeEvents(),
        "ExecutionPlan": SimpleNamespace(
            model_validate=lambda raw: SimpleNamespace(run_id="run-0001")),
    }


CLAIMS = [
    {"verdict": "supported", "evidence_ids": ["ev-1"], "page": 3},
    {"verdict": "unsupported", "evidence_ids": [], "page": None},
]


def _run_layer(monkeypatch, tmp_path, run_status="succeeded", verify_action="review",
               claims=None):
    monkeypatch.setattr(agent_e2e, "_build_runtime",
                        lambda: _fake_runtime(run_status, verify_action,
                                              CLAIMS if claims is None else claims))
    monkeypatch.setenv("RADIANT_E2E_JUDGE_MAX", "0")
    spec = _Spec()
    case = {"case_id": "T-01", "question": "What are the two main components?",
            "gold_anchor": {"page": 3}, "expected_facts": ["encoder"]}
    ctx = LayerContext(run_id="test-run", out_dir=tmp_path, fingerprint_hash="fp-test")
    results = agent_e2e.run_agent_e2e_layer({spec: [case]}, ctx)
    return results["fake_ds"].cases[0], results["fake_ds"]


def test_review_is_not_pass(monkeypatch, tmp_path):
    case, ds = _run_layer(monkeypatch, tmp_path, verify_action="review")
    assert case.status == "review"
    assert ds.metrics["accept_rate"].value == 0.0
    assert ds.metrics["review_rate"].value == 1.0


def test_accept_is_pass(monkeypatch, tmp_path):
    case, ds = _run_layer(monkeypatch, tmp_path, verify_action="accept")
    assert case.status == "pass"
    assert ds.metrics["accept_rate"].value == 1.0
    assert ds.metrics["review_rate"].value == 0.0


def test_failed_run_is_fail(monkeypatch, tmp_path):
    case, ds = _run_layer(monkeypatch, tmp_path, run_status="failed",
                          verify_action=None)
    assert case.status == "fail"
    assert ds.status == "failed"
    assert ds.metrics["execution_rate"].value == 0.0


def test_case_carries_trace_fingerprint_latency(monkeypatch, tmp_path):
    case, ds = _run_layer(monkeypatch, tmp_path, verify_action="review")
    assert case.config_fingerprint == "fp-test"
    assert case.latency_ms is not None
    assert case.trace_uri and Path(case.trace_uri).exists()
    trace = json.loads(Path(case.trace_uri).read_text(encoding="utf-8"))
    assert trace["run_id"] == "run-0001"
    assert trace["events"][0]["type"] == "RUN_STARTED"
    assert case.artifact_uri and Path(case.artifact_uri).exists()
    artifact = json.loads(Path(case.artifact_uri).read_text(encoding="utf-8"))
    assert artifact["verify_action"] == "review"


def test_quality_metrics_present(monkeypatch, tmp_path):
    case, ds = _run_layer(monkeypatch, tmp_path, verify_action="review")
    assert case.metrics["supported_claim_rate"] == 0.5
    assert ds.metrics["supported_claim_rate"].value == 0.5
    assert ds.metrics["anchor_hit_rate"].value == 1.0
    assert ds.metrics["execution_rate"].value == 1.0
