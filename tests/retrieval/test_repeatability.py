"""S11-B: end-to-end retrieval determinism on the real frozen corpus.

Same corpus + same config + same seed must produce byte-identical rankings
across repeated runs AND across independent pipeline rebuilds. Skips
(never fails) when the M0 baseline artifacts are unavailable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import BASELINE_EVIDENCE_DB, BASELINE_VECTOR_STORE

REPO_ROOT = Path(__file__).resolve().parents[2]

# The embedding model must come from the local cache / mirror: a direct
# huggingface.co probe hangs for minutes in this environment (same reason
# the eval adapter pins HF_ENDPOINT in app/eval/adapters.py).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

pytestmark = pytest.mark.skipif(
    not BASELINE_EVIDENCE_DB.exists() or not BASELINE_VECTOR_STORE.exists(),
    reason="M0 baseline artifacts not present")

REPEATS = 3


def _build_pipeline():
    from evidence.store import get_evidence_store
    from retrieval.bm25 import BM25Index, load_text_evidence
    from retrieval.dense import DenseRetriever
    from retrieval.pipeline import RetrievalPipeline, preset_configs

    store = get_evidence_store(str(BASELINE_EVIDENCE_DB))
    items = load_text_evidence(store)
    bm25_index = BM25Index().build(items)
    dense = DenseRetriever(persist_directory=str(BASELINE_VECTOR_STORE),
                           evidence_items=items)
    dense._ensure_store()
    config = preset_configs()["A4_gates"]  # production preset (pinned)
    return RetrievalPipeline(config, bm25_index=bm25_index,
                             dense_retriever=dense, evidence_items=items)


@pytest.fixture(scope="module")
def pipeline():
    return _build_pipeline()


def _sample_cases(n: int = 4) -> list[dict]:
    cases = []
    for line in (REPO_ROOT / "benchmarks" / "retrieval_cases.jsonl"
                 ).read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            if case.get("question"):
                cases.append(case)
        if len(cases) >= n:
            break
    return cases


def _signature(trace: dict) -> dict:
    """Ordered evidence_id list per stage — the full ranking fingerprint."""
    sig = {"final": [c["evidence_id"] for c in trace["final"]]}
    for stage in ("recall_bm25", "recall_dense", "fusion", "rerank"):
        sig[stage] = [c["evidence_id"]
                      for c in trace["stages"][stage]["top_k"]]
    return sig


def test_same_pipeline_repeatable_over_3_runs(pipeline):
    for case in _sample_cases():
        sigs = [_signature(pipeline.run(case["question"]))
                for _ in range(REPEATS)]
        assert sigs[0] == sigs[1] == sigs[2], (
            f"{case['case_id']}: ranking changed across {REPEATS} repeated runs")


def test_independent_rebuild_gives_same_ranking():
    """Index/store rebuilt from scratch must yield the identical ranking
    (catches build-order / filesystem-order / mapping nondeterminism)."""
    case = _sample_cases(n=1)[0]
    sig_a = _signature(_build_pipeline().run(case["question"]))
    sig_b = _signature(_build_pipeline().run(case["question"]))
    assert sig_a == sig_b


def test_stage_trace_carries_per_record_evidence(pipeline):
    """Every stage trace records evidence_id, score, rank and provenance
    per record (S11-B auditability requirement)."""
    trace = pipeline.run(_sample_cases(n=1)[0]["question"])
    for stage in ("recall_bm25", "recall_dense", "fusion", "rerank"):
        top_k = trace["stages"][stage]["top_k"]
        assert top_k, stage
        for record in top_k:
            assert record["evidence_id"] and record["rank"] >= 1
            assert isinstance(record["score"], (int, float))
    fused = trace["stages"]["fusion"]["top_k"][0]
    assert fused["source_ranks"], "fusion records must keep per-lane ranks"
    for record in trace["final"]:
        assert record["evidence_id"] and record["rank"] >= 1
