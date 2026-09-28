"""S1-3: real evidence.search adapter tests.

The adapter wraps the original `direct_jsonl_kb_search` (read-only, no LLM)
and resolves (document_id, chunk_id) -> evidence_id via the Evidence Store.
The real handler is registered only when RADIANT_EVIDENCE_SEARCH_REAL=1
(feature flag; mock stays the default until S1-5).
"""

from __future__ import annotations

import json

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
    {
        "source": "attention_is_all_you_need.pdf",
        "page": 5,
        "chunk_index": 1,
        "chunk_id": "doc-t:p5:c1",
        "content": "Positional encoding uses sine and cosine functions of different frequencies.",
    },
]


@pytest.fixture()
def kb_env(tmp_path, monkeypatch):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    with (kb_dir / "01_chunks_kb.jsonl").open("w", encoding="utf-8") as fh:
        for row in KB_ROWS:
            fh.write(json.dumps(row) + "\n")
    db_path = tmp_path / "evidence.db"
    store = get_evidence_store(db_path)
    try:
        result = store.ingest_directory(kb_dir, workspace_id="default")
        assert result["new_records"] >= 3, result
    finally:
        store.close()
    monkeypatch.setenv("RADIANT_EVIDENCE_KB_DIR", str(kb_dir))
    monkeypatch.setenv("RADIANT_EVIDENCE_DB", str(db_path))
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    # utils.general_utilities (pulled in via pdf_helpers) writes LANGCHAIN_API_KEY
    # into os.environ at import time and crashes on None; service gets it from .env.
    monkeypatch.setenv("LANGCHAIN_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return {"kb_dir": kb_dir, "db_path": db_path}


def _invoke(arguments, workspace="default"):
    registry = build_default_registry()
    return registry.invoke("evidence.search", arguments, run_id="test-run", workspace=workspace)


def test_search_returns_real_evidence_ids(kb_env):
    result = _invoke({"query": "encoder decoder architecture", "top_k": 3})
    assert result.status == ToolStatus.SUCCESS, result.error
    output = result.output
    assert output["mock"] is False
    assert output["hits"], "expected at least one real hit"
    store = get_evidence_store(kb_env["db_path"])
    try:
        for hit in output["hits"]:
            ev = store.get_evidence(hit["evidence_id"])
            assert ev is not None, f"hit {hit['evidence_id']} not resolvable in evidence store"
            assert ev["workspace_id"] == "default"
            assert hit["page"] == ev["page"]
    finally:
        store.close()


def test_search_workspace_isolation(kb_env):
    """Hits must resolve inside the caller's workspace only."""
    result = _invoke({"query": "encoder decoder architecture", "top_k": 3}, workspace="other-ws")
    assert result.status == ToolStatus.SUCCESS, result.error
    assert result.output["hits"] == []
    assert result.output["unresolved_count"] >= 1


def test_search_kb_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("RADIANT_EVIDENCE_SEARCH_REAL", "1")
    monkeypatch.delenv("RADIANT_EVIDENCE_KB_DIR", raising=False)
    monkeypatch.setenv("RADIANT_EVIDENCE_DB", str(tmp_path / "e.db"))
    result = _invoke({"query": "anything", "top_k": 3})
    assert result.status == ToolStatus.TERMINAL_ERROR
    assert result.error.code == "evidence.kb_dir_missing"
    assert result.error.retryable is False


def test_search_mock_remains_default(kb_env, monkeypatch):
    """Without the feature flag the mock handler must stay in place (S1-5 scope)."""
    monkeypatch.delenv("RADIANT_EVIDENCE_SEARCH_REAL", raising=False)
    result = _invoke({"query": "encoder decoder architecture", "top_k": 3})
    assert result.status == ToolStatus.SUCCESS
    assert result.output["mock"] is True
