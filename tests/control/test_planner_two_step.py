"""S1-7C: real read-only QA planner produces a search -> inspect two-step plan.

The inspect step's evidence_ids is a structured step-output reference
(ADR-0003), never a hardcoded Evidence ID. The plan carries no mock content,
no absolute paths, and passes the Schema Guard against the real registry.
"""

from __future__ import annotations

import pytest

from app.control.models import Action, ExecutionPlan, Intent, RouterDecision, ResponseContract
from app.control.planner import RulePlanner
from app.control.schema_guard import SchemaGuard
from app.control.registry import build_default_registry

from tests.control.conftest import code_matches

GOAL = ("What two main components does the Transformer architecture consist of? "
        "Answer with the PDF name, page number and Evidence ID.")


def _decision(intent: Intent = Intent.KNOWLEDGE_QA) -> RouterDecision:
    return RouterDecision(
        intent=intent,
        action=Action.TOOL_CALL,
        confidence=0.9,
        reason_codes=["router.keyword_match"],
        response_contract=ResponseContract(),
    )


@pytest.fixture()
def catalog(monkeypatch):
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    monkeypatch.setenv("RADIANT_EVIDENCE_INSPECT_REAL", "1")
    return build_default_registry().catalog()


def _plan(catalog, goal=GOAL, intent=Intent.KNOWLEDGE_QA) -> ExecutionPlan:
    raw = RulePlanner(tool_catalog=catalog)(_decision(intent), goal)
    return ExecutionPlan.model_validate(raw)


def test_knowledge_qa_plan_is_two_steps_with_binding(catalog) -> None:
    plan = _plan(catalog)
    # S4/S6: the plan is search -> inspect -> context.assemble -> draft -> verify
    assert [s.step_id for s in plan.steps] == [
        "s1-search", "s2-inspect", "s3-context", "s4-draft", "s5-verify"]
    search, inspect, assemble, draft, verify = plan.steps
    assert search.tool == "evidence.search"
    assert search.arguments["query"] == GOAL
    assert search.risk.value == "read_only"
    assert inspect.tool == "evidence.inspect"
    assert inspect.depends_on == ["s1-search"]
    ref = inspect.arguments["evidence_ids"]
    assert ref["ref"] == "step_output"
    assert ref["from_step"] == "s1-search"
    assert ref["path"] == "output.hits[*].evidence_id"
    assert ref["expects"] == "array<string>"
    assert assemble.tool == "context.assemble"
    assert assemble.depends_on == ["s2-inspect"]
    assert assemble.arguments["evidence_records"]["from_step"] == "s2-inspect"
    assert assemble.arguments["question"]["from_step"] == "s1-search"
    assert draft.tool == "answer.draft"
    assert draft.arguments["context_package"]["from_step"] == "s3-context"
    assert draft.arguments["context_package"]["path"] == "output.context_package"
    assert draft.arguments["context_package"]["expects"] == "object"
    assert verify.tool == "answer.verify"
    assert verify.arguments["draft"]["from_step"] == "s4-draft"
    assert verify.arguments["draft"]["path"] == "output.draft"
    assert verify.arguments["evidence_records"]["from_step"] == "s2-inspect"
    assert set(verify.depends_on) >= {"s4-draft", "s2-inspect", "s1-search"}


def test_plan_has_no_gold_no_mock_no_absolute_paths(catalog) -> None:
    plan = _plan(catalog)
    blob = plan.model_dump_json()
    assert "ev-1467122274c2347e3ed99f4c" not in blob  # gold Evidence ID never hardcoded
    assert "mock report body" not in blob
    assert "_MOCK_DOCS" not in blob
    assert "/mnt/" not in blob and "C:\\" not in blob


def test_plan_passes_guard_against_real_registry(catalog, monkeypatch) -> None:
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    monkeypatch.setenv("RADIANT_EVIDENCE_INSPECT_REAL", "1")
    registry = build_default_registry()
    result = SchemaGuard(registry=registry).validate(_plan(catalog))
    assert result.ok, result.reason_codes


def test_plan_budgets_and_timeouts_are_explainable(catalog) -> None:
    plan = _plan(catalog)
    assert plan.budgets.max_tool_calls >= len(plan.steps)
    for step in plan.steps:
        assert step.timeout_ms > 0
        assert step.retry_policy.value in {"none", "transient_only"}


def test_report_request_has_no_mock_report_body(catalog) -> None:
    plan = _plan(catalog, goal="Export a report summarizing the shielding calculations.")
    assert "mock report body" not in plan.model_dump_json()
    assert all(step.tool != "report.export" for step in plan.steps)


def test_visual_qa_gets_same_readonly_chain(catalog) -> None:
    plan = _plan(catalog, intent=Intent.VISUAL_QA)
    assert [s.step_id for s in plan.steps] == [
        "s1-search", "s2-inspect", "s3-context", "s4-draft", "s5-verify"]
