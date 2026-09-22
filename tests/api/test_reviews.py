"""M9 review-queue closed loop: a run paused in waiting_review is approved
through the HTTP API, which resumes the ORIGINAL run to completion."""

from __future__ import annotations

import threading
import uuid

import pytest

from app.control.models import (
    Budgets,
    ExecutionPlan,
    PlanStep,
    RetryPolicy,
    Risk,
    ToolMetrics,
    ToolResult,
    ToolSpec,
    ToolStatus,
)

import api
from conftest import wait_for, wait_run_state


@pytest.fixture()
def gated_tool(runtime):
    calls: list[str] = []

    def handler(arguments, ctx):
        calls.append("invoked")
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"ok": True},
            metrics=ToolMetrics(latency_ms=1, token_count=4),
            provenance=ctx.provenance,
        )

    spec = runtime.registry.get("test.external_gated")
    if spec is None:
        runtime.registry.register(
            ToolSpec(
                name="test.external_gated",
                version="0.0.1-m9-test",
                risk=Risk.EXTERNAL,
                description="M9 API test tool gated behind human review",
                arguments_schema={"type": "object", "properties": {}, "required": []},
                implemented=True,
                handler=handler,
            )
        )
    else:
        spec.handler = handler
    return calls


def _start_gated_run(runtime) -> str:
    plan = ExecutionPlan(
        run_id=uuid.uuid4(),
        goal="api review closed-loop test",
        steps=[
            PlanStep(
                step_id="gated",
                tool="test.external_gated",
                arguments={},
                depends_on=[],
                risk=Risk.EXTERNAL,
                timeout_ms=5_000,
                retry_policy=RetryPolicy.NONE,
            )
        ],
        budgets=Budgets(max_tokens=10_000, max_tool_calls=4, max_wall_time_ms=60_000),
    )
    run_id = str(plan.run_id)
    runtime.plans[run_id] = plan
    runtime.workspaces[run_id] = "default"
    threading.Thread(target=api._execute_plan_bg, args=(runtime, plan, "default"), daemon=True).start()
    return run_id


def test_review_approve_resumes_original_run(client, runtime, gated_tool):
    run_id = _start_gated_run(runtime)
    wait_run_state(client, run_id, {"waiting_review"})

    # the pause enqueued exactly one pending review item for this run
    items = wait_for(
        lambda: (lambda d: d["items"] if d["items"] else None)(client.get("/reviews", params={"run_id": run_id}).json()),
        desc="pending review item",
    )
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "pending"
    assert item["risk_reasons"] == ["policy.external_review:test.external_gated"]
    assert "plan_json" not in item  # never leaked to the list payload
    review_id = item["review_id"]

    # the pending queue endpoint also shows it
    pending = client.get("/reviews").json()
    assert any(i["review_id"] == review_id for i in pending["items"])

    # approve via the API -> original run resumes and completes
    resp = client.post(
        f"/reviews/{review_id}/decision",
        json={"decision": "approve", "reviewer_id": "pytest-reviewer", "rationale": "looks safe"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "decided"
    assert body["resume"] == "started"
    assert body["item"]["status"] == "approved"

    snap = wait_run_state(client, run_id, {"succeeded", "failed", "cancelled"})
    assert snap["state"] == "succeeded"
    assert gated_tool == ["invoked"]  # executed exactly once, only after approval

    # decisions are immutable
    resp = client.post(
        f"/reviews/{review_id}/decision",
        json={"decision": "reject", "reviewer_id": "pytest-reviewer", "rationale": "too late"},
    )
    assert resp.status_code == 409


def test_review_reject_cancels_run(client, runtime, gated_tool):
    run_id = _start_gated_run(runtime)
    wait_run_state(client, run_id, {"waiting_review"})
    items = wait_for(
        lambda: (lambda d: d["items"] if d["items"] else None)(client.get("/reviews", params={"run_id": run_id}).json()),
        desc="pending review item",
    )
    review_id = items[0]["review_id"]
    resp = client.post(
        f"/reviews/{review_id}/decision",
        json={"decision": "reject", "reviewer_id": "pytest-reviewer", "rationale": "not justified"},
    )
    assert resp.status_code == 200
    snap = wait_run_state(client, run_id, {"cancelled", "succeeded", "failed"})
    assert snap["state"] == "cancelled"
    assert gated_tool == []  # rejected run never executed the gated tool


def test_review_decision_errors(client, runtime):
    resp = client.post("/reviews/rv-missing/decision", json={"decision": "approve", "reviewer_id": "x", "rationale": "y"})
    assert resp.status_code == 404

    run_id = _start_gated_run(runtime)
    wait_run_state(client, run_id, {"waiting_review"})
    items = wait_for(
        lambda: (lambda d: d["items"] if d["items"] else None)(client.get("/reviews", params={"run_id": run_id}).json()),
        desc="pending review item",
    )
    review_id = items[0]["review_id"]

    resp = client.post(f"/reviews/{review_id}/decision", json={"decision": "maybe", "reviewer_id": "x", "rationale": "y"})
    assert resp.status_code == 400
    resp = client.post(f"/reviews/{review_id}/decision", json={"decision": "approve", "reviewer_id": "", "rationale": "y"})
    assert resp.status_code == 400
    # edit requires a non-empty edited_answer
    resp = client.post(f"/reviews/{review_id}/decision", json={"decision": "edit", "reviewer_id": "x", "rationale": "y"})
    assert resp.status_code == 409

    # clean up: approve so the run does not linger in waiting_review
    assert client.post(
        f"/reviews/{review_id}/decision",
        json={"decision": "approve", "reviewer_id": "x", "rationale": "cleanup"},
    ).status_code == 200
    wait_run_state(client, run_id, {"succeeded"})
