"""S1-4: real evidence.inspect adapter tests.

Batch lookup by evidence_ids against the Evidence Store, with workspace
enforcement and column-accurate supersede state (D1 fix). Real handler is
registered only when RADIANT_EVIDENCE_INSPECT_REAL=1 (mock default until S1-5).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry
from app.evidence.store import get_evidence_store

KB_ROWS = [
    {
        "source": "attention_is_all_you_need.pdf",
        "page": 1,
        "chunk_index": 1,
        "chunk_id": "doc-t:p1:c1",
        "content": "The Transformer architecture consists of an encoder and a decoder stack.",
    },
    {
        "source": "attention_is_all_you_need.pdf",
        "page": 3,
        "chunk_index": 1,
        "chunk_id": "doc-t:p3:c1",
        "content": "Scaled dot-product attention is computed from queries, keys and values.",
    },
]


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    with (kb_dir / "01_chunks_kb.jsonl").open("w", encoding="utf-8") as fh:
        for row in KB_ROWS:
            fh.write(json.dumps(row) + "\n")
    db_path = tmp_path / "evidence.db"
    store = get_evidence_store(db_path)
    try:
        result = store.ingest_directory(kb_dir, workspace_id="default")
        assert result["new_records"] >= 2, result
        ids = [
            item["evidence_id"]
            for item in store.query_evidence(workspace_id="default")["items"]
        ]
        assert len(ids) >= 2
    finally:
        store.close()
    monkeypatch.setenv("RADIANT_EVIDENCE_DB", str(db_path))
    monkeypatch.setenv("RADIANT_EVIDENCE_INSPECT_REAL", "1")
    return {"db_path": db_path, "ids": ids}


def _invoke(arguments, workspace="default"):
    registry = build_default_registry()
    return registry.invoke("evidence.inspect", arguments, run_id="test-run", workspace=workspace)


def test_inspect_returns_real_evidence(store_env):
    result = _invoke({"evidence_ids": store_env["ids"]})
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.output["mock"] is False
    evidence = result.output["evidence"]
    assert len(evidence) == len(store_env["ids"])
    for ev in evidence:
        assert ev["evidence_id"] in store_env["ids"]
        assert ev["modality"] == "text"
        assert ev["artifact_uri"]
        assert ev["page"] in (1, 3)
        assert ev["source_span"]["chunk_id"]
        assert ev["valid_to"] is None


def test_inspect_not_found(store_env):
    result = _invoke({"evidence_ids": ["ev-does-not-exist"]})
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.output["evidence"] == []
    assert result.output["not_found"] == ["ev-does-not-exist"]


def test_inspect_workspace_mismatch_denied(store_env):
    result = _invoke({"evidence_ids": store_env["ids"]}, workspace="other-ws")
    assert result.status == ToolStatus.DENIED
    assert result.error.code == "evidence.workspace_mismatch"
    assert result.error.retryable is False


def test_inspect_reports_column_valid_to(store_env):
    """D1: supersede updates the column but not the payload; inspect must
    report the column-accurate valid_to."""
    conn = sqlite3.connect(store_env["db_path"])
    conn.execute(
        "UPDATE evidence SET valid_to = '2026-09-23T00:00:00+00:00' WHERE evidence_id = ?",
        (store_env["ids"][0],),
    )
    conn.commit()
    conn.close()
    result = _invoke({"evidence_ids": [store_env["ids"][0]]})
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.output["evidence"][0]["valid_to"] == "2026-09-23T00:00:00+00:00"


def test_inspect_mock_remains_default(store_env, monkeypatch):
    monkeypatch.delenv("RADIANT_EVIDENCE_INSPECT_REAL", raising=False)
    result = _invoke({"doc_id": "doc-001"})
    assert result.status == ToolStatus.SUCCESS
    assert result.output["mock"] is True
