"""S3-2: chunk -> evidence_id resolution must be column-accurate and unified
on the *currently valid* version (fixes D8: superseded rows used to win by
dict-order accident because the payload JSON never carries valid_to).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry
from app.evidence.store import get_evidence_store

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)

OLD_EID = "ev-old000000000000001"
NEW_EID = "ev-new000000000000001"


def _supersede_chunk(db_path, chunk_id="doc-t:p1:c1") -> None:
    """Simulate a re-ingest: close the old version's validity, insert the
    current version of the same chunk with a new evidence_id."""
    conn = sqlite3.connect(str(db_path))
    old_payload = conn.execute(
        "SELECT payload, document_id, document_version, content_hash, modality,"
        " page, section, figure_id, degraded, degraded_reason, authority_level,"
        " artifact_uri, parser_fingerprint FROM evidence WHERE"
        " json_extract(payload, '$.source_span.chunk_id') = ?",
        (chunk_id,),
    ).fetchone()
    payload = json.loads(old_payload[0])
    conn.execute(
        "UPDATE evidence SET valid_to = ? WHERE evidence_id = ?",
        ("2026-09-01T00:00:00", payload["evidence_id"]),
    )
    new_payload = dict(payload, evidence_id=NEW_EID, document_version="v2-current")
    conn.execute(
        "INSERT INTO evidence (evidence_id, workspace_id, document_id,"
        " document_version, content_hash, modality, page, section, figure_id,"
        " degraded, degraded_reason, authority_level, artifact_uri,"
        " parser_fingerprint, payload, valid_from, valid_to)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (NEW_EID, payload["workspace_id"], old_payload[1], "v2-current",
         "hash-v2", old_payload[4], old_payload[5], old_payload[6], old_payload[7],
         old_payload[8], old_payload[9], old_payload[10], old_payload[11],
         old_payload[12], json.dumps(new_payload), "2026-09-24T00:00:00", None),
    )
    conn.commit()
    conn.close()
    return payload["evidence_id"]


def test_query_evidence_current_only_filters_superseded(kb_env) -> None:
    _supersede_chunk(kb_env["db_path"])
    store = get_evidence_store(kb_env["db_path"])
    try:
        all_items = store.query_evidence(workspace_id="default")["items"]
        current = store.query_evidence(workspace_id="default", current_only=True)["items"]
    finally:
        store.close()
    ids_all = {it["evidence_id"] for it in all_items}
    ids_cur = {it["evidence_id"] for it in current}
    assert NEW_EID in ids_cur
    assert ids_all - ids_cur  # superseded rows exist in the unfiltered view
    assert NEW_EID not in (ids_all - ids_cur)


def test_current_evidence_by_chunk_returns_only_current(kb_env) -> None:
    _supersede_chunk(kb_env["db_path"])
    store = get_evidence_store(kb_env["db_path"])
    try:
        by_chunk = store.current_evidence_by_chunk(workspace_id="default")
    finally:
        store.close()
    assert by_chunk["doc-t:p1:c1"]["evidence_id"] == NEW_EID
    assert len(by_chunk) >= 3


def test_search_resolves_to_current_version(kb_env) -> None:
    old_eid = _supersede_chunk(kb_env["db_path"])
    registry = build_default_registry()
    result = registry.invoke(
        "evidence.search",
        {"query": "What does the Transformer architecture consist of?", "top_k": 5},
        run_id="s3-2", workspace="default",
    )
    assert result.status == ToolStatus.SUCCESS
    hit_ids = [h["evidence_id"] for h in result.output.get("hits", [])]
    assert NEW_EID in hit_ids, hit_ids
    assert old_eid not in hit_ids
