"""S5: governed memory over the real API -- Write Gate default-deny, Read Gate
workspace isolation, explicit supersede, audited delete, and the
context.assemble memory feed.
"""

from __future__ import annotations

import pytest

from conftest import wait_for, wait_run_state

PREF_BODY = {
    "category": "user_fact",
    "subject": "preferred model",
    "value": "DeepSeek",
    "workspace": "default",
    "write_reason": "user explicitly stated preference",
    "confidence": 0.95,
    "user_confirmed": True,
    "user_confirmation_id": "confirm-001",
    "provenance": {"origin": "user", "source_session_id": "sess-1", "user_confirmation_id": "confirm-001"},
}


def test_write_confirmed_fact_allowed(client):
    resp = client.post("/memories", json=dict(PREF_BODY, subject="preferred model t1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "allow"
    assert body["memory_id"]


def test_default_deny_without_confirmation(client):
    body = dict(PREF_BODY, user_confirmed=False, user_confirmation_id=None)
    body["provenance"] = {"origin": "model", "source_session_id": "sess-1"}
    resp = client.post("/memories", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "reject"
    assert "missing_user_confirmation" in data["reasons"]
    assert data.get("memory_id") is None


def test_evidence_as_user_fact_rejected(client):
    body = dict(PREF_BODY)
    body["provenance"] = dict(PREF_BODY["provenance"], evidence_id="ev-xyz")
    resp = client.post("/memories", json=body)
    data = resp.json()
    assert data["outcome"] == "reject"
    assert "evidence_as_user_fact" in data["reasons"]


def test_read_gate_workspace_isolation(client):
    client.post("/memories", json=dict(PREF_BODY, subject="preferred model t4"))
    hit = client.get("/memories", params={"q": "preferred model DeepSeek", "workspace": "default"})
    assert any(r["value"] == "DeepSeek" for r in hit.json()["records"])
    other = client.get("/memories", params={"q": "preferred model DeepSeek", "workspace": "other-ws"})
    assert all(r["value"] != "DeepSeek" for r in other.json()["records"])


def test_explicit_supersede_no_silent_overwrite(client):
    client.post("/memories", json=dict(PREF_BODY, subject="preferred model t5"))
    conflict = client.post("/memories", json=dict(PREF_BODY, subject="preferred model t5", value="Qwen"))
    assert conflict.json()["outcome"] in ("conflict_review", "conflict_clarify")
    # explicit supersede path: confirmed new value replaces the old
    listing = client.get("/memories", params={"workspace": "default"}).json()
    old = [r for r in listing["records"] if r["subject"] == "preferred model t5"][0]
    resp = client.post(
        f"/memories/{old['memory_id']}/supersede",
        json=dict(PREF_BODY, subject="preferred model t5", value="Qwen"),
    )
    assert resp.status_code == 200, resp.text
    after = client.get("/memories", params={"workspace": "default"}).json()
    t5_values = {r["value"] for r in after["records"] if r["subject"] == "preferred model t5"}
    assert t5_values == {"Qwen"}  # superseded value never recalled again


def test_delete_is_audited(client):
    created = client.post("/memories", json=dict(PREF_BODY, subject="preferred model t6")).json()
    mid = created["memory_id"]
    resp = client.delete(f"/memories/{mid}", params={"reason": "user requested removal", "actor": "user"})
    assert resp.status_code == 200
    audit = client.get(f"/memories/{mid}/audit").json()
    actions = [a["action"] for a in audit["events"]]
    assert "delete" in actions
    # deleted record no longer recallable
    hit = client.get("/memories", params={"q": "DeepSeek", "workspace": "default"})
    assert all(r["memory_id"] != mid for r in hit.json()["records"])


def test_context_assemble_memory_feed(client):
    client.post("/memories", json=dict(PREF_BODY, subject="Transformer answer style",
                                       value="cite encoder and decoder explicitly"))
    records = [{"evidence_id": "ev-x", "document_id": "d", "page": 1, "modality": "text",
                "content": "Transformer encoder decoder content", "source_span": {"chunk_id": "d:p1:c0"}}]
    resp = client.post(
        "/runs",
        json={"goal": "noop"},  # placeholder; direct tool invoke below instead
    )
    # call context.assemble through the runs runtime registry directly
    from api import get_run_runtime
    rt = get_run_runtime()
    result = rt.registry.invoke(
        "context.assemble",
        {"evidence_records": records, "question": "Transformer answer style"},
        run_id="s5-mem", workspace="default",
    )
    pkg = result.output["context_package"]
    memory_text = pkg["items"] and "" or ""
    mem_items = [i for i in pkg["items"] if i["partition"] == "memory"]
    assert mem_items, "memory partition must carry the recalled memory"
    assert pkg["usage"]["memory"] > 0


# ---------------------------------------------------------------------------
# S10: post-answer governed memory write (hook on the answer chain)
# ---------------------------------------------------------------------------

def test_accepted_run_writes_session_memory(client):
    """A run whose verifier accepts the answer automatically produces a
    Write-Gate-passed session summary + evidence pointers."""
    from api import get_memory_runtime

    resp = client.post("/runs", json={"goal": "search evidence and export report",
                                      "workspace": "default"})
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    snap = wait_run_state(client, run_id, {"succeeded", "failed"})
    assert snap["state"] == "succeeded"

    # the post-answer hook runs in the background thread AFTER the run
    # completes -- poll the store instead of assuming synchronous timing
    store = get_memory_runtime().store
    summaries = wait_for(
        lambda: store.find_active(workspace="default", category="session",
                                  subject=f"run_summary:{run_id}"),
        desc="post-answer session memory")
    assert len(summaries) == 1
    assert summaries[0].provenance.source_run_id == run_id
    assert summaries[0].ttl_seconds is not None

    # every claim was supported (see test_runs.test_create_run_completes), so
    # the cited fixture evidence gets a pointer
    pointers = wait_for(
        lambda: store.find_active(workspace="default", category="evidence_pointer",
                                  subject="evidence_cited:ev-fixture0000000000000001"),
        desc="post-answer evidence pointer")
    assert len(pointers) == 1
    assert pointers[0].provenance.evidence_id == "ev-fixture0000000000000001"


def test_post_answer_hook_gated_and_review_silent(client, monkeypatch):
    """The hook only fires on SUCCEEDED + accept; the Write Gate stays
    default-deny; hook failures never break the answer path."""
    from types import SimpleNamespace

    import api as api_module
    from app.api import _maybe_write_memory
    from app.durable.graph import RunState

    plan = SimpleNamespace(run_id="run-review-only", goal="q")
    report = SimpleNamespace(
        status=RunState.SUCCEEDED,
        results={"s5-verify": SimpleNamespace(output={
            "verify_action": "review", "answer": "a", "claims": []})})
    _maybe_write_memory(api_module.get_run_runtime(), plan, "default", report)
    store = api_module.get_memory_runtime().store
    assert store.find_active(workspace="default", category="session",
                             subject="run_summary:run-review-only") == []

    def boom(**kwargs):
        raise RuntimeError("candidate generation exploded")

    monkeypatch.setattr("app.memory.candidates.candidates_from_run", boom)
    report_accept = SimpleNamespace(
        status=RunState.SUCCEEDED,
        results={"s5-verify": SimpleNamespace(output={
            "verify_action": "accept", "answer": "a", "claims": []})})
    # must not raise
    _maybe_write_memory(api_module.get_run_runtime(), plan, "default", report_accept)
