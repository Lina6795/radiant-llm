"""Observability: unified trace adapters on real layer data + bad-case registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.durable.events import EventStore, EventType
from app.observability.bad_cases import BadCase, BadCaseRegistry, new_case
from app.observability.trace import (
    SCHEMA_VERSION,
    TraceWriter,
    from_context_decision,
    from_durable_events,
    from_retrieval_trace,
    read_traces,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
M4_TRACE = (REPO_ROOT / "artifacts/retrieval/m4-20260922/cases/A4_gates"
            "/RET-T01.json")


class TestRetrievalTraceAdapter:
    """Adapter over a real M4 per-case trace artifact."""

    @pytest.fixture()
    def records(self):
        if not M4_TRACE.is_file():
            pytest.skip("M4 trace artifact not present")
        payload = json.loads(M4_TRACE.read_text(encoding="utf-8"))
        return from_retrieval_trace(
            payload["trace"], run_id="m4-20260922", case_id="RET-T01",
            artifact_uri=str(M4_TRACE))

    def test_every_stage_mapped(self, records):
        stages = {r.stage for r in records}
        assert {"recall_bm25", "recall_dense", "fusion", "filters",
                "rerank", "gates", "grader", "final"} <= stages

    def test_schema_fields(self, records):
        for record in records:
            data = record.to_dict()
            assert data["schema_version"] == SCHEMA_VERSION
            assert data["run_id"] == "m4-20260922"
            assert data["layer"] == "retrieval"
            assert data["case_id"] == "RET-T01"
            assert data["config_fingerprint"] == "7a4085cd0b0520ac"
            assert data["artifact_uri"] == str(M4_TRACE)

    def test_latencies_preserved(self, records):
        by_stage = {r.stage: r for r in records}
        assert by_stage["final"].latency_ms == pytest.approx(45.469, abs=0.01)
        assert by_stage["recall_bm25"].latency_ms is not None


class TestDurableEventAdapter:
    """Adapter over a real M3 EventStore stream."""

    @pytest.fixture()
    def records(self, tmp_path):
        store = EventStore(str(tmp_path / "events.db"))
        store.append("run-1", EventType.RUN_STARTED, {"goal": "g"})
        store.append("run-1", EventType.NODE_STARTED, {"step_id": "s1"})
        store.append("run-1", EventType.NODE_RETRIED,
                     {"step_id": "s1", "attempt": 2})
        store.append("run-1", EventType.NODE_COMPLETED, {"step_id": "s1"})
        store.append("run-1", EventType.RUN_COMPLETED, {"status": "succeeded"})
        events = store.stream("run-1")
        store.close()
        return from_durable_events(events, case_id="RT-01",
                                   config_fingerprint="fp")

    def test_order_and_stages(self, records):
        assert [r.stage for r in records] == [
            "run_started", "node_started", "node_retried",
            "node_completed", "run_completed"]
        assert [r.extra["seq"] for r in records] == [1, 2, 3, 4, 5]

    def test_fields(self, records):
        for record in records:
            assert record.run_id == "run-1"
            assert record.layer == "durable"
            assert record.case_id == "RT-01"
            assert record.ts is not None
            assert record.config_fingerprint == "fp"

    def test_payload_summaries(self, records):
        retried = records[2]
        assert "step_id=s1" in retried.outputs_summary
        assert "attempt=2" in retried.outputs_summary

    def test_dict_events_accepted(self, records):
        dicts = [r.to_dict() for r in records]
        mapped = from_durable_events(
            [{"run_id": "run-1", "type": "node_failed", "seq": 6,
              "payload": {"step_id": "s2", "error": "boom"},
              "created_at": 1.0}])
        assert mapped[0].status == "error"
        assert mapped[0].extra["error"] == "boom"
        assert dicts  # sanity


class TestContextDecisionAdapter:
    """Adapter over a real M5 engine decision record."""

    @pytest.fixture()
    def record(self, tmp_path):
        from app.context.budgets import BudgetConfig
        from app.context.engine import ContextEngine
        from app.context.selector import EvidenceItem

        config = BudgetConfig(
            total_tokens=4096, response_reserve=512,
            quotas={"system": 256, "active_turn": 256, "memory": 256,
                    "evidence": 1024, "artifact": 256, "tool_result": 256})
        engine = ContextEngine(config=config)
        memory = "memory note sentence with several words in it. " * 60
        result = engine.assemble(
            system="sys", active_turn="q", memory=memory,
            evidence=[EvidenceItem(evidence_id="ev-1", content="fact",
                                   page=1, score=0.9)])
        return from_context_decision(
            result.decision.to_dict(), run_id="m5-test", case_id="CTX-T02",
            config_fingerprint="fp")

    def test_decision_mapped(self, record):
        assert record.layer == "context"
        assert record.stage == "assemble"
        assert "decision=compress" in record.outputs_summary
        assert record.case_id == "CTX-T02"

    def test_compression_lineage_preserved(self, record):
        lineage = record.extra["compression_lineage"]
        assert lineage
        for entry in lineage:
            assert entry["input_hash"] and entry["output_hash"]
            assert entry["method"]
            assert entry["tokens_before"] > entry["tokens_after"]


class TestTraceWriter:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        writer = TraceWriter(path)
        records = from_durable_events(
            [{"run_id": "r", "type": "run_started", "seq": 1,
              "payload": {}, "created_at": 1.0}])
        assert writer.write(records) == 1
        back = read_traces(path)
        assert len(back) == 1
        assert back[0].run_id == "r"
        assert back[0].stage == "run_started"


class TestBadCaseRegistry:
    def test_add_get_update_persist(self, tmp_path):
        path = tmp_path / "bad_cases.json"
        registry = BadCaseRegistry(path)
        case = new_case(registry, layer="retrieval", source="trace:m4",
                        symptom="RET-T02 anchor missed at rank 21",
                        root_cause="RRF k too small")
        registry.add(case)
        assert case.bad_case_id == "BC-retrieval-001"

        registry.update("BC-retrieval-001", status="fixed",
                        fix_commit="abc123", regression_case="RET-T02")
        reloaded = BadCaseRegistry(path)
        loaded = reloaded.get("BC-retrieval-001")
        assert loaded.status == "fixed"
        assert loaded.fix_commit == "abc123"
        assert loaded.regression_case == "RET-T02"
        assert loaded.attribution_layer == "retrieval"

    def test_duplicate_add_rejected(self, tmp_path):
        registry = BadCaseRegistry(tmp_path / "bc.json")
        registry.add(new_case(registry, layer="memory", source="audit",
                              symptom="leak"))
        with pytest.raises(ValueError):
            registry.add(new_case(registry, layer="memory", source="audit",
                                  symptom="leak", sequence=1))

    def test_unknown_update_rejected(self, tmp_path):
        registry = BadCaseRegistry(tmp_path / "bc.json")
        with pytest.raises(KeyError):
            registry.update("BC-nope-001", status="fixed")

    def test_status_validated(self):
        with pytest.raises(ValueError):
            BadCase(bad_case_id="BC-x-001", found_date="2026-09-22",
                    source="audit", symptom="s", attribution_layer="control",
                    status="bogus")

    def test_attribution_required(self):
        with pytest.raises(ValueError):
            BadCase(bad_case_id="BC-x-001", found_date="2026-09-22",
                    source="audit", symptom="s", attribution_layer="")

    def test_filter_and_export_ledger(self, tmp_path):
        registry = BadCaseRegistry(tmp_path / "bc.json")
        registry.add(new_case(registry, layer="durable", source="review",
                              symptom="resume lost checkpoint"))
        registry.add(new_case(registry, layer="retrieval", source="trace",
                              symptom="anchor miss"))
        assert len(registry.list(status="open")) == 2
        assert len(registry.list(layer="durable")) == 1
        ledger = registry.export_ledger()
        assert "| BC-durable-001 |" in ledger
        assert "归因层" in ledger
