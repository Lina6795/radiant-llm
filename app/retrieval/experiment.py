"""M4 ablation experiment runner (A0-A4) on the frozen M0 baseline corpus.

One-command reproduction:

    cd /mnt/lina/radiant-llm && \\
      RADIANT_EVIDENCE_DB=artifacts/baseline/m0-20260922/evidence.db \\
      RADIANT_VECTOR_STORE=artifacts/baseline/m0-20260922/output/local_vector_store \\
      HF_ENDPOINT=https://hf-mirror.com \\
      ./runtime/bin/python3.12 -m retrieval.experiment \\
        --output-dir artifacts/retrieval/m4-20260922

Paths come from CLI flags or env vars only; nothing is hardcoded.
Writes metrics.json, per-config per-case traces, and paired diffs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence.store import get_evidence_store  # noqa: E402
from retrieval.bm25 import BM25Index, load_text_evidence  # noqa: E402
from retrieval.dense import DenseRetriever  # noqa: E402
from retrieval.pipeline import RetrievalPipeline, preset_configs  # noqa: E402

CONFIG_ORDER = ["A0_dense_only", "A1_bm25_dense_merge", "A2_rrf",
                "A3_rrf_proxy_rerank", "A4_gates"]


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def load_cases(path: Path) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def build_docname_map(items: List[Dict[str, Any]]) -> Dict[str, str]:
    """source filename -> document_id (via artifact_uri basename)."""
    out: Dict[str, str] = {}
    for it in items:
        uri = it.get("artifact_uri") or ""
        name = os.path.basename(uri)
        if name and it.get("document_id"):
            out[name] = it["document_id"]
    return out


def relevant_set(case: Dict[str, Any], items: List[Dict[str, Any]],
                 docname_map: Dict[str, str]) -> set:
    anchor = case.get("gold_anchor")
    if not anchor:
        return set()
    doc_id = docname_map.get(anchor.get("document", ""))
    rel = set()
    for it in items:
        if doc_id and it.get("document_id") != doc_id:
            continue
        if anchor.get("page") is not None and it.get("page") != anchor["page"]:
            continue
        rel.add(it["evidence_id"])
    return rel


def first_relevant_rank(trace_final: List[Dict[str, Any]], rel: set) -> Optional[int]:
    for entry in trace_final:
        if entry["evidence_id"] in rel:
            return entry["rank"]
    return None


def evaluate_case(trace: Dict[str, Any], rel: set, ks=(5, 20)) -> Dict[str, Any]:
    final = trace["final"]
    ids_top = {k: [e["evidence_id"] for e in final if e["rank"] <= k] for k in ks}
    out: Dict[str, Any] = {
        "grader_verdict": trace["grader_verdict"],
        "latency_ms": trace["total_latency_ms"],
        "n_final": len(final),
    }
    if not rel:
        return out
    rank = first_relevant_rank(final, rel)
    out["first_relevant_rank"] = rank
    out["rr"] = (1.0 / rank) if rank else 0.0
    for k in ks:
        hits = len(rel & set(ids_top[k]))
        out[f"recall@{k}"] = hits / len(rel)
        out[f"anchor_hit@{k}"] = 1.0 if hits > 0 else 0.0
    # binary-gain nDCG@max(ks)
    k_max = max(ks)
    dcg = sum(1.0 / math.log2(e["rank"] + 1)
              for e in final if e["rank"] <= k_max and e["evidence_id"] in rel)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k_max) + 1))
    out[f"ndcg@{k_max}"] = (dcg / idcg) if idcg else 0.0
    return out


def aggregate(per_case: List[Dict[str, Any]]) -> Dict[str, Any]:
    anchored = [c for c in per_case if "rr" in c]
    lat = [c["latency_ms"] for c in per_case]
    def mean(key: str) -> Optional[float]:
        vals = [c[key] for c in anchored if key in c]
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "n_cases": len(per_case),
        "n_anchored_cases": len(anchored),
        "recall@5": mean("recall@5"),
        "recall@20": mean("recall@20"),
        "anchor_hit@5": mean("anchor_hit@5"),
        "anchor_hit@20": mean("anchor_hit@20"),
        "mrr": mean("rr"),
        "ndcg@20": mean("ndcg@20"),
        "latency_p50_ms": round(percentile(lat, 50), 2) if lat else None,
        "latency_p95_ms": round(percentile(lat, 95), 2) if lat else None,
        "grader_verdicts": {v: sum(1 for c in per_case if c["grader_verdict"] == v)
                            for v in ("enough", "retrieve_more", "conflict", "abstain")},
    }


def paired_diff(results: Dict[str, Dict[str, Dict[str, Any]]],
                a: str, b: str) -> Dict[str, List[str]]:
    improved, worse, unchanged = [], [], []
    for case_id, ra in results[a].items():
        rb = results[b].get(case_id)
        if rb is None or "rr" not in ra or "rr" not in rb:
            continue
        ka = ra["first_relevant_rank"] or 10 ** 9
        kb = rb["first_relevant_rank"] or 10 ** 9
        if kb < ka:
            improved.append(case_id)
        elif kb > ka:
            worse.append(case_id)
        else:
            unchanged.append(case_id)
    return {"pair": f"{a}->{b}", "improved": sorted(improved),
            "worse": sorted(worse), "unchanged": sorted(unchanged)}


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=Path,
                    default=repo_root / "benchmarks" / "retrieval_cases.jsonl")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--evidence-db", default=os.getenv("RADIANT_EVIDENCE_DB"))
    ap.add_argument("--vector-store", default=os.getenv("RADIANT_VECTOR_STORE"))
    ap.add_argument("--recall-k", type=int, default=20)
    args = ap.parse_args()
    if not args.evidence_db:
        ap.error("--evidence-db or RADIANT_EVIDENCE_DB required")
    if not args.vector_store:
        ap.error("--vector-store or RADIANT_VECTOR_STORE required")

    store = get_evidence_store(args.evidence_db)
    items = load_text_evidence(store)
    print(f"[m4] loaded {len(items)} authoritative text evidence rows",
          flush=True)

    t0 = time.perf_counter()
    bm25_index = BM25Index().build(items)
    print(f"[m4] BM25 index built over {bm25_index.size} docs "
          f"in {time.perf_counter() - t0:.2f}s", flush=True)

    dense = DenseRetriever(persist_directory=args.vector_store,
                           evidence_items=items)
    dense._ensure_store()  # load embedding model once, outside latency timing
    probe = dense.query("attention mechanism", k=5)
    mapped = [c for c in probe if not c.evidence_id.startswith("unmapped:")]
    if not mapped:
        raise RuntimeError(
            "dense probe returned no mappable hits — vector store path wrong "
            "or store empty; aborting before measuring garbage metrics")
    print(f"[m4] dense vector store ready (probe: {len(probe)} hits)", flush=True)

    docname_map = build_docname_map(items)
    cases = load_cases(args.cases)
    out_dir: Path = args.output_dir
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)

    configs = preset_configs()
    results: Dict[str, Dict[str, Dict[str, Any]]] = {}
    metrics: Dict[str, Any] = {
        "milestone": "M4",
        "date": "2026-09-22",
        "corpus": {
            "evidence_db": str(Path(args.evidence_db).name),
            "n_text_evidence": len(items),
            "recall_k": args.recall_k,
        },
        "configs": {},
    }

    for name in CONFIG_ORDER:
        cfg = configs[name]
        cfg.recall_k = args.recall_k
        pipeline = RetrievalPipeline(cfg, bm25_index=bm25_index,
                                     dense_retriever=dense,
                                     evidence_items=items)
        results[name] = {}
        per_case: List[Dict[str, Any]] = []
        for case in cases:
            anchor = case.get("gold_anchor")
            if anchor and anchor.get("document") in docname_map:
                anchor = {**anchor,
                          "document_id": docname_map[anchor["document"]]}
            trace = pipeline.run(case["question"], anchor=anchor)
            rel = relevant_set(case, items, docname_map)
            ev = evaluate_case(trace, rel)
            ev["case_id"] = case["case_id"]
            per_case.append(ev)
            results[name][case["case_id"]] = ev
            trace_path = out_dir / "cases" / name / f"{case['case_id']}.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(json.dumps(
                {"case": case, "metrics": ev, "trace": trace},
                ensure_ascii=False, indent=2), encoding="utf-8")
        agg = aggregate(per_case)
        agg["config_fingerprint"] = cfg.fingerprint()
        metrics["configs"][name] = agg
        print(f"[m4] {name}: anchor_hit@20={agg['anchor_hit@20']} "
              f"mrr={agg['mrr']} p50={agg['latency_p50_ms']}ms", flush=True)

    diffs = []
    for a, b in zip(CONFIG_ORDER, CONFIG_ORDER[1:]):
        diffs.append(paired_diff(results, a, b))
    diffs.append(paired_diff(results, CONFIG_ORDER[0], CONFIG_ORDER[-1]))
    metrics["paired_diffs"] = diffs

    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "paired_diff.json").write_text(
        json.dumps(diffs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[m4] wrote {out_dir / 'metrics.json'}", flush=True)


if __name__ == "__main__":
    main()
