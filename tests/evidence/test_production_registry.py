"""S1-5: production registry must wire real evidence handlers by default.

`build_default_registry(evidence="real")` is what RunRuntime uses; the
no-arg default stays mock so the existing test baseline is untouched.
"""

from __future__ import annotations

import json

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry
from app.evidence.store import get_evidence_store

KB_ROW = {
    "source": "attention_is_all_you_need.pdf",
    "page": 1,
    "chunk_index": 1,
    "chunk_id": "doc-t:p1:c1",
    "content": "The Transformer architecture consists of an encoder and a decoder stack.",
}


@pytest.fixture()
def real_env(tmp_path, monkeypatch):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    with (kb_dir / "01_chunks_kb.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(KB_ROW) + "\n")
    db_path = tmp_path / "evidence.db"
    store = get_evidence_store(db_path)
    try:
        store.ingest_directory(kb_dir, workspace_id="default")
    finally:
        store.close()
    monkeypatch.setenv("RADIANT_EVIDENCE_KB_DIR", str(kb_dir))
    monkeypatch.setenv("RADIANT_EVIDENCE_DB", str(db_path))
    monkeypatch.delenv("RADIANT_EVIDENCE_SEARCH_REAL", raising=False)
    monkeypatch.delenv("RADIANT_EVIDENCE_INSPECT_REAL", raising=False)
    monkeypatch.setenv("LANGCHAIN_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return {"db_path": db_path}


def test_production_registry_registers_real_handlers(real_env):
    registry = build_default_registry(evidence="real")
    assert registry.get("evidence.search").version == "evidence.search/2"
    assert registry.get("evidence.inspect").version == "evidence.inspect/2"

    search = registry.invoke(
        "evidence.search", {"query": "encoder decoder", "top_k": 3}, run_id="t", workspace="default"
    )
    assert search.status == ToolStatus.SUCCESS, search.error
    assert search.output["mock"] is False
    evidence_id = search.output["hits"][0]["evidence_id"]

    inspect = registry.invoke(
        "evidence.inspect", {"evidence_ids": [evidence_id]}, run_id="t", workspace="default"
    )
    assert inspect.status == ToolStatus.SUCCESS, inspect.error
    assert inspect.output["mock"] is False
    assert inspect.output["evidence"][0]["page"] == 1


def test_default_registry_stays_mock(real_env):
    registry = build_default_registry()
    search = registry.invoke(
        "evidence.search", {"query": "encoder decoder", "top_k": 3}, run_id="t", workspace="default"
    )
    assert search.output["mock"] is True
