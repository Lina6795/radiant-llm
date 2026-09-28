"""S9-1/S9-2: /query defaults to the control chain for knowledge QA; the
legacy convchain is reachable only through an explicit switch; a new-chain
failure must surface a typed error, never a silent fallback to the old chain.
"""

from __future__ import annotations


def test_query_knowledge_defaults_to_control_chain(client):
    resp = client.post("/query", json={"query": "search evidence for reactor safety and export report"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chain"] == "control"
    assert body["run_id"]
    assert body["verify_action"] in ("accept", "review")
    assert body["claims"]
    assert body["router_reason_codes"]
    assert "convchain" not in body


def test_query_legacy_switch_explicit(client):
    resp = client.post("/query", json={"query": "Hello, what can you do?", "legacy": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("chain") == "legacy"
    assert "response" in body


def test_query_non_tool_intent_stays_legacy_with_disclosure(client):
    resp = client.post("/query", json={"query": "Hello!"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("chain") == "legacy"
    router = body.get("router") or {}
    assert router.get("action") in ("respond", "clarify")
    assert router.get("reason_codes")


def test_query_control_chain_failure_is_typed_not_fallback(client, monkeypatch):
    # force the control chain to fail AFTER router selects tool_call:
    # break the KB dir for this request only -> evidence.kb_dir_missing must
    # surface as a typed error, not a silent convchain response.
    import os

    monkeypatch.setenv("RADIANT_EVIDENCE_KB_DIR", "/nonexistent-kb-dir")
    resp = client.post("/query", json={"query": "search evidence for reactor safety"})
    assert resp.status_code != 200 or resp.json().get("chain") == "control"
    body = resp.json()
    if resp.status_code == 200:
        assert body["chain"] == "control"
        assert body.get("error", {}).get("code") == "evidence.kb_dir_missing"
    else:
        detail = body.get("detail") or {}
        assert "evidence.kb_dir_missing" in str(detail)
