"""Human Review Queue: enqueue -> decide -> resume of the ORIGINAL run.

The closed loop is exercised against the real M3 DurableRunner: a run that
pauses in waiting_review must resume to completion (approve/edit) or to a
cancelled terminal state (reject) with its completed steps restored from
checkpoints — never re-executed, never a new run_id.
"""

from __future__ import annotations

import pytest

from app.durable.events import EventType
from app.durable.graph import RunState
from app.control.models import Risk
from app.control.registry import ToolRegistry

from app.verification.review import ReviewDecision, ReviewQueue, ReviewStatus, ScriptedReviewer

from tests.review.conftest import (
    Stores,
    make_answer_plan,
    make_runner,
    ok_tool,
    register_tool,
)

CLAIMS = [
    {"claim_id": "cl-1", "text": "The model achieved 28.4 BLEU.", "status": "supported"},
    {"claim_id": "cl-2", "text": "Latency was 12 ms.", "status": "unsupported"},
]
EVIDENCE_SNAPSHOT = [{"evidence_id": "ev-1", "page": 8, "content": "...28.4 BLEU..."}]
RISK_REASONS = ["verify.unsupported_claim"]


def _registry_with_answer_tools(queue: ReviewQueue, draft_calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    register_tool(
        registry, "answer.draft",
        ok_tool({"answer": "The model achieved 28.4 BLEU."}, calls=draft_calls, name="draft"),
    )

    def finalize(arguments, ctx):
        # Edit path: the reviewer edits the draft in the review store; the
        # resumed run reads it back through the tool's data path, so the plan
        # (and its config fingerprint) never changes.
        answer = "The model achieved 28.4 BLEU."
        for item in queue.for_run(ctx.run_id):
            if item["status"] == ReviewStatus.EDITED.value and item.get("edited_answer"):
                answer = item["edited_answer"]
        return ok_tool({"final_answer": answer})(arguments, ctx)

    register_tool(registry, "answer.finalize", finalize, risk=Risk.EXTERNAL)
    return registry


def _paused_run(stores: Stores, queue: ReviewQueue):
    draft_calls: list[str] = []
    registry = _registry_with_answer_tools(queue, draft_calls)
    runner = make_runner(
        stores, registry, require_review=lambda step: step.tool == "answer.finalize"
    )
    plan = make_answer_plan()
    report = runner.run(plan)
    assert report.status is RunState.WAITING_REVIEW
    assert report.reason == "waiting_review:finalize"
    return runner, registry, plan, report, draft_calls


def _cleared_runner(stores: Stores, registry) -> object:
    """Runner with the review gate cleared by the recorded decision — the
    same idiom as M3's own approval-resume test (tests/durable/test_resume.py)."""
    return make_runner(stores, registry, require_review=lambda step: False)


def _enqueue(queue: ReviewQueue, plan) -> str:
    return queue.enqueue(
        run_id=str(plan.run_id),
        claims=CLAIMS,
        evidence_snapshot=EVIDENCE_SNAPSHOT,
        risk_reasons=RISK_REASONS,
        plan=plan,
        metadata={"case_id": "AN-TEST"},
    )


def test_enqueue_and_pending(stores, review_queue) -> None:
    runner, registry, plan, report, _ = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)

    pending = review_queue.pending()
    assert [p["review_id"] for p in pending] == [review_id]
    item = review_queue.get(review_id)
    assert item["run_id"] == str(plan.run_id)
    assert item["claims"] == CLAIMS
    assert item["evidence_snapshot"] == EVIDENCE_SNAPSHOT
    assert item["risk_reasons"] == RISK_REASONS
    assert item["status"] == ReviewStatus.PENDING.value
    assert item["plan_json"]


def test_approve_resumes_original_run(stores, review_queue) -> None:
    runner, registry, plan, report, draft_calls = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)

    decided = review_queue.decide(
        review_id, decision=ReviewDecision.APPROVE,
        reviewer_id="reviewer-1", rationale="claims check out",
    )
    assert decided["status"] == ReviewStatus.APPROVED.value
    assert decided["reviewer_id"] == "reviewer-1"

    resumed = review_queue.resume_decided(review_id, _cleared_runner(stores, registry))
    assert resumed.run_id == str(plan.run_id)          # same run, not a new task
    assert resumed.status is RunState.SUCCEEDED
    assert resumed.resume.resumed is True
    assert resumed.resume.restored_steps == ["draft"]  # completed step restored
    assert draft_calls == ["draft"]                    # ... and never re-executed
    assert resumed.results["finalize"].output["final_answer"] == "The model achieved 28.4 BLEU."

    events = [e.type for e in stores.events.stream(str(plan.run_id))]
    assert EventType.WAITING_REVIEW in events
    assert EventType.RUN_RESUMED in events
    assert EventType.RUN_COMPLETED in events


def test_reject_cancels_original_run(stores, review_queue) -> None:
    runner, registry, plan, report, draft_calls = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)

    review_queue.decide(
        review_id, decision=ReviewDecision.REJECT,
        reviewer_id="reviewer-1", rationale="unsupported numeric claim",
    )
    resumed = review_queue.resume_decided(review_id, _cleared_runner(stores, registry))
    assert resumed.run_id == str(plan.run_id)
    assert resumed.status is RunState.CANCELLED
    assert draft_calls == ["draft"]

    events = [e.type for e in stores.events.stream(str(plan.run_id))]
    assert EventType.RUN_CANCELLED in events
    record = stores.checkpoints.get_run(str(plan.run_id))
    assert record.state is RunState.CANCELLED


def test_edit_flows_into_resumed_run(stores, review_queue) -> None:
    runner, registry, plan, report, draft_calls = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)

    review_queue.decide(
        review_id, decision=ReviewDecision.EDIT,
        reviewer_id="reviewer-1", rationale="removed unsupported latency claim",
        edited_answer="The model achieved 28.4 BLEU (verified).",
    )
    resumed = review_queue.resume_decided(review_id, _cleared_runner(stores, registry))
    assert resumed.status is RunState.SUCCEEDED
    assert resumed.results["finalize"].output["final_answer"] == "The model achieved 28.4 BLEU (verified)."


def test_decisions_are_immutable(stores, review_queue) -> None:
    runner, registry, plan, _, _ = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)
    review_queue.decide(
        review_id, decision=ReviewDecision.APPROVE, reviewer_id="r1", rationale="ok",
    )
    with pytest.raises(ValueError, match="immutable"):
        review_queue.decide(
            review_id, decision=ReviewDecision.REJECT, reviewer_id="r2", rationale="changed mind",
        )


def test_edit_requires_answer_and_pending_resume_rejected(stores, review_queue) -> None:
    runner, registry, plan, _, _ = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)
    with pytest.raises(ValueError, match="edited_answer"):
        review_queue.decide(
            review_id, decision=ReviewDecision.EDIT, reviewer_id="r1", rationale="x",
        )
    with pytest.raises(ValueError, match="pending"):
        review_queue.resume_decided(review_id, runner)


def test_scripted_reviewer_approve_and_replay(stores, review_queue) -> None:
    runner, registry, plan, _, _ = _paused_run(stores, review_queue)
    review_id = review_queue.enqueue(
        run_id=str(plan.run_id),
        claims=[c for c in CLAIMS if c["status"] == "supported"],
        evidence_snapshot=EVIDENCE_SNAPSHOT,
        risk_reasons=[],
        plan=plan,
    )
    reviewer = ScriptedReviewer()
    decided = reviewer.apply(
        review_queue, review_id,
        expected_facts=["28.4 BLEU"], answer="The model achieved 28.4 BLEU.",
    )
    assert decided["decision"] == ReviewDecision.APPROVE.value
    assert reviewer.replay(review_queue.get(review_id)) == ReviewDecision.APPROVE.value


def test_scripted_reviewer_rejects_unsupported(stores, review_queue) -> None:
    runner, registry, plan, _, _ = _paused_run(stores, review_queue)
    review_id = _enqueue(review_queue, plan)
    reviewer = ScriptedReviewer()
    decided = reviewer.apply(
        review_queue, review_id,
        expected_facts=["28.4 BLEU"], answer="The model achieved 28.4 BLEU and ran at 12 ms.",
    )
    assert decided["decision"] == ReviewDecision.REJECT.value
    assert "cl-2" in decided["rationale"]
    assert reviewer.replay(review_queue.get(review_id)) == ReviewDecision.REJECT.value


def test_scripted_reviewer_rejects_missing_facts(stores, review_queue) -> None:
    runner, registry, plan, _, _ = _paused_run(stores, review_queue)
    review_id = review_queue.enqueue(
        run_id=str(plan.run_id),
        claims=[c for c in CLAIMS if c["status"] == "supported"],
        evidence_snapshot=EVIDENCE_SNAPSHOT,
        risk_reasons=[],
        plan=plan,
    )
    reviewer = ScriptedReviewer()
    decided = reviewer.apply(
        review_queue, review_id,
        expected_facts=["28.4 BLEU", "h=8"], answer="The model achieved 28.4 BLEU.",
    )
    assert decided["decision"] == ReviewDecision.REJECT.value
    assert "h=8" in decided["rationale"]
