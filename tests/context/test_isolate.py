"""Isolation: oversized tool output lands on disk, pointer stays in context."""

import hashlib
from pathlib import Path

import pytest

from context.isolate import ArtifactStore

CONTENT = "row_id,metric,value\n" + "\n".join(
    f"{i},latency_ms,{i * 1.5:.2f}" for i in range(2000))


def test_store_writes_file_and_pointer(tmp_path):
    store = ArtifactStore(root=tmp_path)
    ptr = store.store(CONTENT, kind="tool_result")
    path = Path(ptr.uri[len("file://"):])
    assert path.exists()
    assert ptr.sha256 == hashlib.sha256(CONTENT.encode()).hexdigest()
    assert ptr.size_bytes == len(CONTENT.encode())
    assert ptr.original_tokens > 0
    assert ptr.summary and len(ptr.summary) <= 161
    assert ptr.kind == "tool_result"


def test_pointer_stub_is_compact(tmp_path):
    store = ArtifactStore(root=tmp_path)
    ptr = store.store(CONTENT)
    stub = ptr.stub()
    assert len(stub) < len(CONTENT) // 10
    assert ptr.sha256[:12] in stub and ptr.uri in stub


def test_resolve_round_trip(tmp_path):
    store = ArtifactStore(root=tmp_path)
    ptr = store.store(CONTENT)
    assert store.resolve(ptr) == CONTENT


def test_resolve_detects_tampering(tmp_path):
    store = ArtifactStore(root=tmp_path)
    ptr = store.store(CONTENT)
    Path(ptr.uri[len("file://"):]).write_text("tampered")
    with pytest.raises(ValueError):
        store.resolve(ptr, verify=True)
    assert store.resolve(ptr, verify=False) == "tampered"


def test_content_addressed_dedup(tmp_path):
    store = ArtifactStore(root=tmp_path)
    p1 = store.store(CONTENT)
    p2 = store.store(CONTENT)
    assert p1.uri == p2.uri
    assert len(list(tmp_path.iterdir())) == 1


def test_env_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIANT_ARTIFACT_DIR", str(tmp_path / "envdir"))
    store = ArtifactStore()
    ptr = store.store(CONTENT)
    assert str(tmp_path / "envdir") in ptr.uri
