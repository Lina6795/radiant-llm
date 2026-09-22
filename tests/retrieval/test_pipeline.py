"""End-to-end pipeline test on fully synthetic components (offline)."""

from retrieval.bm25 import BM25Index
from retrieval.pipeline import RetrievalConfig, RetrievalPipeline, preset_configs
from retrieval.types import Candidate


class FakeDense:
    """Deterministic stand-in for DenseRetriever (no model, no I/O)."""

    def __init__(self, ranking):
        self.ranking = ranking

    def query(self, question, k=20):
        out = []
        for rank, (eid, dist) in enumerate(self.ranking[:k], start=1):
            out.append(Candidate(evidence_id=eid, score=-dist, rank=rank,
                                 source="dense", raw_score=dist,
                                 source_ranks={"dense": {"rank": rank,
                                                         "score": -dist}}))
        return out


def build_pipeline(sample_evidence, cfg):
    bm25 = BM25Index().build(sample_evidence)
    dense = FakeDense([("ev-a2", 0.1), ("ev-a1", 0.2), ("ev-a3", 0.9)])
    return RetrievalPipeline(cfg, bm25_index=bm25, dense_retriever=dense,
                             evidence_items=sample_evidence)


def test_pipeline_trace_structure(sample_evidence):
    cfg = RetrievalConfig(name="t", use_bm25=True, use_dense=True)
    trace = build_pipeline(sample_evidence, cfg).run("attention mechanisms")
    for stage in ("recall_bm25", "recall_dense", "fusion", "filters",
                  "rerank", "gates", "grader"):
        assert stage in trace["stages"], stage
    assert trace["config_fingerprint"]
    assert trace["grader_verdict"] in {"enough", "retrieve_more", "conflict",
                                       "abstain"}
    assert trace["total_latency_ms"] >= 0
    for entry in trace["final"]:
        assert "evidence_id" in entry and "source_ranks" in entry


def test_pipeline_dense_only_config(sample_evidence):
    cfg = RetrievalConfig(name="t0", use_bm25=False, use_dense=True,
                          fusion="dense_priority")
    trace = build_pipeline(sample_evidence, cfg).run("attention")
    assert "recall_bm25" not in trace["stages"]
    ids = [e["evidence_id"] for e in trace["final"]]
    assert ids[:3] == ["ev-a2", "ev-a1", "ev-a3"]  # dense order preserved


def test_pipeline_anchor_gate_surfaces_in_trace(sample_evidence):
    cfg = RetrievalConfig(name="t4", use_bm25=True, use_dense=True)
    cfg.anchor_gate.enabled = True
    cfg.anchor_gate.apply_authority_weight = False
    cfg.anchor_gate.protect_top_k = 1
    trace = build_pipeline(sample_evidence, cfg).run(
        "regularization", anchor={"page": 3, "document_id": "docA"})
    protected = trace["stages"]["gates"]["anchor_protection"]
    assert any(p["evidence_id"] == "ev-a4" for p in protected) or \
        trace["final"][0]["evidence_id"] == "ev-a4"


def test_preset_configs_fingerprints_differ():
    presets = preset_configs()
    fps = {c.fingerprint() for c in presets.values()}
    assert len(fps) == len(presets)
    assert presets["A0_dense_only"].use_bm25 is False
    assert presets["A3_rrf_proxy_rerank"].rerank.enabled is True
    assert presets["A4_gates"].anchor_gate.enabled is True
