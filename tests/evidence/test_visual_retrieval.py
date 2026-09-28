"""S7: real visual evidence -- retrieval includes visual modality, modality
quota protects visual presence, and the Visual Fact Gate rejects text-only
"visual support".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.control.models import ToolStatus
from app.control.registry import build_default_registry
from app.retrieval.types import Candidate

from tests.evidence.test_search_adapter import kb_env  # noqa: F401 (fixture reuse)

VISUAL_RECORD = {
    "evidence_id": "ev-visual00000000001",
    "document_id": "doc-t",
    "page": 3,
    "modality": "visual",
    "figure_id": "doc-t:p3:f0",
    "degraded": True,
    "degraded_reason": "missing_region_bbox",
    "content": "Block diagram with encoder half on the left and decoder half on the right, Figure 1 architecture.",
    "source_span": {"chunk_id": None, "figure_index": 0},
}


def _seed_visual(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    template = conn.execute("SELECT payload FROM evidence LIMIT 1").fetchone()[0]
    payload = json.loads(template)
    conn.execute(
        "INSERT INTO evidence (evidence_id, workspace_id, document_id,"
        " document_version, content_hash, modality, page, section, figure_id,"
        " degraded, degraded_reason, authority_level, artifact_uri,"
        " parser_fingerprint, payload, valid_from, valid_to)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (VISUAL_RECORD["evidence_id"], "default", payload["document_id"], "v1", "hv",
         "visual", 3, None, VISUAL_RECORD["figure_id"], 1, "missing_region_bbox",
         "primary", None, "fp", json.dumps(VISUAL_RECORD | {"workspace_id": "default",
                                                           "document_id": payload["document_id"]}),
         "2026-09-24T00:00:00", None),
    )
    conn.commit()
    conn.close()


def _run(kb_env, query, top_k=3):
    from app.evidence.hybrid_search import hybrid_search, _cache
    from app.evidence.store import get_evidence_store

    class EmptyDense:
        def query(self, q, k=None):
            return []

    _cache.clear()
    store = get_evidence_store(kb_env["db_path"])
    try:
        return hybrid_search(query, top_k, "default", store,
                             dense_factory=lambda items: EmptyDense())
    finally:
        store.close()


def test_visual_items_enter_index_and_are_retrievable(kb_env) -> None:
    _seed_visual(kb_env["db_path"])
    result = _run(kb_env, "What does Figure 1 architecture diagram show with encoder and decoder?")
    modalities = {h["evidence_id"]: h.get("modality") for h in result["hits"]}
    assert VISUAL_RECORD["evidence_id"] in modalities
    assert modalities[VISUAL_RECORD["evidence_id"]] == "visual"


def test_modality_quota_reserves_visual_slot(kb_env) -> None:
    _seed_visual(kb_env["db_path"])
    # text-heavy query: lexical overlap pulls text chunks ahead, but the
    # visual candidate must not be crowded out of top_k entirely
    result = _run(kb_env, "transformer architecture encoder decoder figure", top_k=3)
    visual_hits = [h for h in result["hits"] if h.get("modality") == "visual"]
    assert visual_hits, result["hits"]
    assert result.get("modality_quota_notes") is not None


def test_visual_fact_gate(kb_env, monkeypatch) -> None:
    from app.verification import answer_tools

    text_record = {"evidence_id": "ev-t1", "document_id": "d", "page": 3, "modality": "text",
                   "content": "Figure 1 shows the encoder and decoder architecture diagram.",
                   "source_span": {"chunk_id": "d:p3:c0"}}
    monkeypatch.setattr(answer_tools, "_chat", answer_tools.StubLLM() if hasattr(answer_tools, "StubLLM") else (lambda m, **k: {}))
    registry = build_default_registry()

    draft = "Figure 1 shows the encoder and decoder architecture [ev-t1]."
    result = registry.invoke(
        "answer.verify",
        {"draft": draft, "evidence_records": [text_record], "question": "q", "allow_revise": False},
        run_id="s7", workspace="default",
    )
    assert result.status == ToolStatus.SUCCESS, result.error
    visual_claims = [c for c in result.output["claims"] if c["claim_type"] == "visual"]
    assert visual_claims
    # text evidence must NOT count as visual proof
    assert all(c["verdict"] != "supported" for c in visual_claims)

    visual_record = dict(text_record, evidence_id="ev-v1", modality="visual", figure_id="d:p3:f0")
    draft2 = "Figure 1 shows the encoder and decoder architecture [ev-v1]."
    result2 = registry.invoke(
        "answer.verify",
        {"draft": draft2, "evidence_records": [visual_record], "question": "q", "allow_revise": False},
        run_id="s7", workspace="default",
    )
    assert result2.status == ToolStatus.SUCCESS, result2.error
    visual_claims2 = [c for c in result2.output["claims"] if c["claim_type"] == "visual"]
    assert visual_claims2 and all(c["verdict"] == "supported" for c in visual_claims2)
