"""S6-6: high-risk answer verification pauses into the Review Queue with the
REAL claims/evidence payload, and an approve decision resumes the original run.
"""

from __future__ import annotations

import time

import app.verification.answer_tools as answer_tools

from conftest import wait_run_state

NUMERIC_REPLY = {
    "content": "The fixture uses exactly 6 encoder layers [ev-fixture0000000000000001].",
    "latency_ms": 1, "prompt_tokens": 10, "completion_tokens": 5,
}


def _wait_review(client, run_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = client.get("/reviews", params={"run_id": run_id}).json()["items"]
        pending = [i for i in items if i["status"] == "pending"]
        if pending:
            return pending[0]
        time.sleep(0.3)
    return None


def test_numeric_answer_pauses_with_real_payload_then_approve_resumes(client):
    original = answer_tools._chat
    answer_tools._chat = lambda messages, **kw: NUMERIC_REPLY
    try:
        run_id = client.post(
            "/runs", json={"goal": "search evidence for reactor safety"}
        ).json()["run_id"]

        item = _wait_review(client, run_id)
        assert item is not None, "run never paused into the review queue"
        # real payload: claims from the draft, evidence snapshot from inspect
        assert item["claims"], "review item must carry the draft claims"
        assert any(c.get("claim_type") in ("numeric", "unit") for c in item["claims"])
        assert item["evidence_snapshot"], "review item must carry the evidence snapshot"
        assert item["evidence_snapshot"][0]["evidence_id"]

        snap = client.get(f"/runs/{run_id}").json()
        assert snap["state"] == "waiting_review"

        resp = client.post(
            f"/reviews/{item['review_id']}/decision",
            json={"decision": "approve", "reviewer_id": "test-reviewer"},
        )
        assert resp.status_code == 200, resp.text
        final = wait_run_state(client, run_id, {"succeeded", "failed"}, timeout=30)
        assert final["state"] == "succeeded"
        verify = {s["step_id"]: s for s in final["steps"]}["s5-verify"]
        assert verify["state"] == "succeeded"
        assert verify["output"]["claims"]
    finally:
        answer_tools._chat = original


def test_numeric_answer_reject_cancels_run(client):
    original = answer_tools._chat
    answer_tools._chat = lambda messages, **kw: NUMERIC_REPLY
    try:
        run_id = client.post(
            "/runs", json={"goal": "search evidence and export report"}
        ).json()["run_id"]
        item = _wait_review(client, run_id)
        assert item is not None
        resp = client.post(
            f"/reviews/{item['review_id']}/decision",
            json={"decision": "reject", "reviewer_id": "test-reviewer", "rationale": "unsupported numeric claim"},
        )
        assert resp.status_code == 200, resp.text
        final = wait_run_state(client, run_id, {"cancelled", "succeeded"}, timeout=30)
        assert final["state"] == "cancelled"
    finally:
        answer_tools._chat = original
