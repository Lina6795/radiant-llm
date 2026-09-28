"""S1-1: frozen data contracts for the first vertical slice (read-only PDF QA).

Five samples pinned here are the authoritative contract examples; the same
samples live in docs/CONTRACTS_SLICE1.md. If either side changes, this test
file must change in the same commit.
"""

from __future__ import annotations

import uuid

import pytest

from app.control.models import ExecutionPlan, RouterDecision, ToolResult
from app.evidence.models import Evidence

# TEACH-T01: benchmarks/teaching_case.jsonl
QUESTION = (
    "What two main components does the Transformer architecture consist of? "
    "Answer with the PDF name, page number and Evidence ID."
)

ROUTER_DECISION_SAMPLE = {
    "intent": "knowledge_qa",
    "action": "tool_call",
    "confidence": 0.85,
    "working_subject": None,
    "reason_codes": ["router.keyword_match"],
    "response_contract": {"format": "answer", "citation_required": True},
}

EXECUTION_PLAN_SAMPLE = {
    "run_id": str(uuid.uuid4()),
    "goal": QUESTION,
    "steps": [
        {
            "step_id": "s1-search",
            "tool": "evidence.search",
            "arguments": {"query": QUESTION, "top_k": 3, "workspace_id": "default"},
            "depends_on": [],
            "risk": "read_only",
            "timeout_ms": 5000,
            "retry_policy": "transient_only",
        },
        {
            "step_id": "s2-inspect",
            "tool": "evidence.inspect",
            "arguments": {"evidence_ids": ["ev-1467122274c2347e3ed99f4c"]},
            "depends_on": ["s1-search"],
            "risk": "read_only",
            "timeout_ms": 5000,
            "retry_policy": "transient_only",
        },
    ],
    "budgets": {"max_tokens": 8000, "max_tool_calls": 4, "max_wall_time_ms": 30000},
}

TOOL_RESULT_SAMPLE = {
    "status": "success",
    "output": {
        "hits": [
            {
                "evidence_id": "ev-1467122274c2347e3ed99f4c",
                "document": "attention_is_all_you_need_1706.03762.pdf",
                "page": 1,
                "score": 0.83,
            }
        ],
        "mock": False,
    },
    "artifacts": [],
    "error": None,
    "metrics": {"latency_ms": 42, "token_count": 0},
    "provenance": {"tool_version": "evidence.search/2", "config_fingerprint": "sha256:abc"},
}

EVIDENCE_SAMPLE = {
    "evidence_id": "ev-1467122274c2347e3ed99f4c",
    "workspace_id": "default",
    "document_id": "e8a365c1d8815226",
    "document_version": "v-891411cca8dc-e56f6c",
    "content_hash": "891411cca8dcb4c23161bd8f8865631f777285cfc1f04f5cb1da292837258cb4",
    "page": 1,
    "section": None,
    "figure_id": None,
    "region_bbox": None,
    "modality": "text",
    "source_span": {
        "chunk_id": "e8a365c1d8815226:p1:c2",
        "prev_chunk_id": "e8a365c1d8815226:p1:c1",
        "next_chunk_id": "e8a365c1d8815226:p1:c3",
        "figure_index": None,
        "char_start": None,
        "char_end": None,
    },
    "artifact_uri": "file:///mnt/lina/radiant-llm/artifacts/baseline/m0-vision-deepseek/output/attention_is_all_you_need_1706.03762.pdf",
    "parser_fingerprint": "evidence-adapter/1;extractor=nougat;vlm=deepseek-flash",
    "authority_level": "primary",
    "valid_from": "2026-09-22T11:18:46.109907+00:00",
    "valid_to": None,
    "degraded": False,
    "degraded_reason": None,
    "content": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include a",
    "metadata_fields": {},
}


def test_query_request_contract_exists():
    """Gap test for S1-1: the slice has no request contract yet."""
    from app.control.models import QueryRequest

    req = QueryRequest(question=QUESTION, workspace_id="default")
    assert req.question == QUESTION
    assert req.workspace_id == "default"
    assert req.session_id is None


def test_router_decision_sample_validates():
    decision = RouterDecision.model_validate(ROUTER_DECISION_SAMPLE)
    assert decision.action.value == "tool_call"
    assert decision.response_contract.citation_required is True


def test_execution_plan_sample_validates():
    plan = ExecutionPlan.model_validate(EXECUTION_PLAN_SAMPLE)
    assert [s.tool for s in plan.steps] == ["evidence.search", "evidence.inspect"]
    assert plan.steps[1].depends_on == ["s1-search"]
    assert all(s.risk.value == "read_only" for s in plan.steps)


def test_tool_result_sample_validates():
    result = ToolResult.model_validate(TOOL_RESULT_SAMPLE)
    assert result.status.value == "success"
    assert result.output["mock"] is False
    assert result.output["hits"][0]["evidence_id"].startswith("ev-")


def test_evidence_sample_validates():
    evidence = Evidence.model_validate(EVIDENCE_SAMPLE)
    assert evidence.evidence_id == "ev-1467122274c2347e3ed99f4c"
    assert evidence.page == 1
    assert evidence.modality.value == "text"
    assert evidence.valid_to is None
