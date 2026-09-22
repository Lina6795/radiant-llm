"""Real-baseline smoke test: existence checks only, skips (never fails)
when the M0 artifacts are unavailable."""

import json
from pathlib import Path

import pytest

from conftest import BASELINE_EVIDENCE_DB, BASELINE_VECTOR_STORE

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not BASELINE_EVIDENCE_DB.exists() or not BASELINE_VECTOR_STORE.exists(),
    reason="M0 baseline artifacts not present")


def test_real_corpus_smoke():
    from evidence.store import get_evidence_store
    from retrieval.bm25 import BM25Index, load_text_evidence
    from retrieval.dense import build_chunk_to_evidence_map

    store = get_evidence_store(str(BASELINE_EVIDENCE_DB))
    res = store.query_evidence(modality="text", limit=1000)
    assert res["total"] >= 100, f"expected >=100 text evidence, got {res['total']}"
    assert all(not it["degraded"] for it in res["items"])

    items = load_text_evidence(store)
    idx = BM25Index().build(items)
    assert idx.size == len(items)
    hits = idx.query("scaled dot product attention", top_k=5)
    assert hits and hits[0].score > 0

    by_chunk, _ = build_chunk_to_evidence_map(items)
    assert len(by_chunk) == len(items)

    # vector store directory exists with a chroma sqlite inside
    assert (BASELINE_VECTOR_STORE / "chroma.sqlite3").exists()

    # frozen retrieval cases exist and every anchored case resolves to
    # at least one evidence row on its gold page
    cases_path = REPO_ROOT / "benchmarks" / "retrieval_cases.jsonl"
    if cases_path.exists():
        docname = {it["artifact_uri"].rsplit("/", 1)[-1]: it["document_id"]
                   for it in items if it.get("artifact_uri")}
        anchored = 0
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            anchor = case.get("gold_anchor")
            if not anchor:
                continue
            anchored += 1
            doc_id = docname.get(anchor["document"])
            rel = [it for it in items
                   if it["document_id"] == doc_id
                   and it.get("page") == anchor["page"]]
            assert rel, f"{case['case_id']}: no evidence on gold page {anchor['page']}"
        assert anchored >= 12
