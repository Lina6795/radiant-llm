"""S3-4: metadata/version/workspace gate after fusion, before anything reaches
the prompt -- every dropped candidate carries an explainable reason.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.retrieval.types import Candidate

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)

OTHER_EID = "ev-otherws00000000001"
DEGRADED_EID = "ev-degraded0000000001"


def _seed_extra_rows(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    template = conn.execute("SELECT payload FROM evidence LIMIT 1").fetchone()[0]
    payload = json.loads(template)

    def _row(eid, workspace, degraded):
        p = dict(payload, evidence_id=eid, workspace_id=workspace, degraded=degraded,
                 source_span={"chunk_id": f"{eid}:p1:c0", "page": 1})
        conn.execute(
            "INSERT INTO evidence (evidence_id, workspace_id, document_id,"
            " document_version, content_hash, modality, page, section, figure_id,"
            " degraded, degraded_reason, authority_level, artifact_uri,"
            " parser_fingerprint, payload, valid_from, valid_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, workspace, payload["document_id"], "v1", "h", "text", 1, None, None,
             int(degraded), "ocr_failed" if degraded else None, "primary", None,
             "fp", json.dumps(p), "2026-09-24T00:00:00", None),
        )

    _row(OTHER_EID, "other", False)
    _row(DEGRADED_EID, "default", True)
    conn.commit()
    conn.close()
    return payload["evidence_id"]


class CrossWorkspaceDense:
    def __init__(self, items):
        self._ids = [it["evidence_id"] for it in items]

    def query(self, question, k=None):
        out = []
        for rank, eid in enumerate([OTHER_EID, DEGRADED_EID] + self._ids, start=1):
            out.append(Candidate(evidence_id=eid, score=1.0 / rank, rank=rank, source="dense"))
        return out[: k or 20]


def test_gate_drops_with_reasons(kb_env) -> None:
    from app.evidence.hybrid_search import hybrid_search
    from app.evidence.store import get_evidence_store

    first_id = _seed_extra_rows(kb_env["db_path"])
    store = get_evidence_store(kb_env["db_path"])
    try:
        result = hybrid_search("transformer", 5, "default", store,
                               dense_factory=lambda items: CrossWorkspaceDense(items))
    finally:
        store.close()

    dropped = {d["evidence_id"]: d["reasons"] for d in result["gate"]["dropped"]}
    assert OTHER_EID in dropped
    assert any("workspace" in r or "no_evidence_record" in r for r in dropped[OTHER_EID]), dropped
    assert DEGRADED_EID in dropped
    assert any("degraded" in r for r in dropped[DEGRADED_EID]), dropped

    hit_ids = [h["evidence_id"] for h in result["hits"]]
    assert OTHER_EID not in hit_ids and DEGRADED_EID not in hit_ids
    assert first_id in hit_ids
    assert result["gate"]["dropped_count"] >= 2
