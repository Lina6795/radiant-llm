"""S4-5: context.assemble tool and the three-step production plan."""

from __future__ import annotations

import pytest

from app.control.models import Action, ExecutionPlan, Intent, RouterDecision, ResponseContract, ToolStatus
from app.control.planner import RulePlanner
from app.control.registry import build_default_registry
from app.control.schema_guard import SchemaGuard

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)

RECORDS = [
    {"evidence_id": "ev-a", "document_id": "doc1", "page": 1, "modality": "text",
     "content": "alpha " * 60, "source_span": {"chunk_id": "doc1:p1:c1"}},
    {"evidence_id": "ev-b", "document_id": "doc1", "page": 1, "modality": "text",
     "content": "beta " * 60, "source_span": {"chunk_id": "doc1:p1:c2"}},
    {"evidence_id": "ev-c", "document_id": "doc2", "page": 3, "modality": "text",
     "content": "gamma " * 60, "source_span": {"chunk_id": "doc2:p3:c0"}},
]


def _invoke(arguments, kb_env, workspace="default"):
    registry = build_default_registry()
    return registry.invoke("context.assemble", arguments, run_id="s4-5", workspace=workspace)


def test_assemble_emits_package_with_trace(kb_env) -> None:
    result = _invoke({"evidence_records": RECORDS, "question": "q"}, kb_env)
    assert result.status == ToolStatus.SUCCESS, result.error
    pkg = result.output["context_package"]
    assert result.output["mock"] is False
    assert pkg["package_fingerprint"]
    assert set(pkg["budgets"]) >= {"evidence", "active_turn", "memory"}
    ev_items = [i for i in pkg["items"] if i["partition"] == "evidence"]
    assert {i["evidence_id"] for i in ev_items} == {"ev-a", "ev-b", "ev-c"}
    assert pkg["usage"]["evidence"] > 0
    assert pkg["decision"] in ("assemble", "compress")


def test_invalid_input_is_terminal(kb_env) -> None:
    result = _invoke({"evidence_records": [{"no_id": 1}]}, kb_env)
    assert result.status == ToolStatus.TERMINAL_ERROR
    assert result.error.code == "context.invalid_input"
    assert result.error.retryable is False


def test_tight_budget_drops_with_reasons_and_pin_survives(kb_env) -> None:
    result = _invoke(
        {
            "evidence_records": RECORDS,
            "question": "q",
            "evidence_budget_tokens": 80,
            "pinned_evidence_ids": ["ev-a"],
        },
        kb_env,
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    pkg = result.output["context_package"]
    kept = {i["evidence_id"] for i in pkg["items"] if i["partition"] == "evidence"}
    assert "ev-a" in kept  # pinned anchor never shed silently
    drop_ids = {d["item_id"] for d in pkg["drops"]}
    assert kept | drop_ids >= {"ev-a", "ev-b", "ev-c"} - {"__pinned_set__"}
    assert pkg["decision"] in ("assemble", "compress", "abstain")


def test_neighbor_expansion_uses_current_store(kb_env) -> None:
    result = _invoke({"evidence_records": RECORDS[:1], "question": "q"}, kb_env)
    assert result.status == ToolStatus.SUCCESS, result.error
    prep = result.output["prep"]
    # kb fixture has no doc1 chunks, so no neighbors; contract = notes list exists
    assert isinstance(prep["neighbor_notes"], list)


def test_planner_emits_three_step_plan(kb_env, monkeypatch) -> None:
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    monkeypatch.setenv("RADIANT_EVIDENCE_INSPECT_REAL", "1")
    registry = build_default_registry()
    planner = RulePlanner(tool_catalog=registry.catalog())
    decision = RouterDecision(
        intent=Intent.KNOWLEDGE_QA, action=Action.TOOL_CALL, confidence=0.9,
        reason_codes=["router.keyword_match"], response_contract=ResponseContract(),
    )
    raw = planner(decision, "What does the Transformer consist of? Cite Evidence ID.")
    plan = ExecutionPlan.model_validate(raw)
    # S6: the plan continues through the bounded answer chain
    assert [s.step_id for s in plan.steps] == [
        "s1-search", "s2-inspect", "s3-context", "s4-draft", "s5-verify"]
    ctx_step = plan.steps[2]
    assert ctx_step.tool == "context.assemble"
    ref = ctx_step.arguments["evidence_records"]
    assert ref["ref"] == "step_output" and ref["from_step"] == "s2-inspect"
    assert ref["path"] == "output.evidence" and ref["expects"] == "array<object>"
    q = ctx_step.arguments["question"]
    assert q["from_step"] == "s1-search" and q["path"] == "output.query" and q["expects"] == "string"
    draft_step = plan.steps[3]
    assert draft_step.tool == "answer.draft"
    assert draft_step.arguments["context_package"]["from_step"] == "s3-context"
    verify_step = plan.steps[4]
    assert verify_step.tool == "answer.verify"
    assert verify_step.arguments["draft"]["from_step"] == "s4-draft"
    result = SchemaGuard(registry=registry).validate(plan)
    assert result.ok, result.reason_codes
