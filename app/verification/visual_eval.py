"""RADIANT-Control M7: visual retrieval evaluation scaffold.

Metrics over text-to-visual and visual-to-text retrieval results:

* ``recall_at_k``        — fraction of gold figure_ids found in the top-K
* ``region_recall_at_k`` — fraction of gold regions (page + bbox) matched by
                           a retrieved region on the same page with IoU >=
                           threshold

The real run entry point (:func:`run_visual_eval`) requires a parsed
``02_visuals_kb.jsonl`` and a retrieval-results file. When either is
missing it returns an explicit ``deferred`` status — never fabricated
numbers. Synthetic fixtures exercise the metric math in tests.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

DEFAULT_KS = (1, 3, 5, 10)


def recall_at_k(ranked: Sequence[str], gold: Set[str], k: int) -> float:
    if not gold:
        return 0.0
    hits = set(ranked[:k]) & gold
    return round(len(hits) / len(gold), 4)


def bbox_iou(a: Dict[str, float], b: Dict[str, float]) -> float:
    x0, y0 = max(a["x0"], b["x0"]), max(a["y0"], b["y0"])
    x1, y1 = min(a["x1"], b["x1"]), min(a["y1"], b["y1"])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a["x1"] - a["x0"]) * max(0.0, a["y1"] - a["y0"])
    area_b = max(0.0, b["x1"] - b["x0"]) * max(0.0, b["y1"] - b["y0"])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def region_recall_at_k(
    ranked_regions: Sequence[Dict[str, Any]],
    gold_regions: Sequence[Dict[str, Any]],
    k: int,
    *,
    iou_threshold: float = 0.5,
) -> float:
    """Gold region = {"page": int, "bbox": {x0,y0,x1,y1}}. A gold region is
    recalled when any top-K retrieved region shares its page and overlaps
    with IoU >= threshold."""
    if not gold_regions:
        return 0.0
    recalled = 0
    for gold in gold_regions:
        for cand in list(ranked_regions)[:k]:
            if cand.get("page") != gold.get("page"):
                continue
            if bbox_iou(cand.get("bbox") or {}, gold.get("bbox") or {}) >= iou_threshold:
                recalled += 1
                break
    return round(recalled / len(gold_regions), 4)


def evaluate_visual_retrieval(
    results: Dict[str, Sequence[str]],
    gold: Dict[str, Iterable[str]],
    ks: Sequence[int] = DEFAULT_KS,
) -> Dict[str, Any]:
    """Aggregate visual Recall@K over a case set.

    ``results``: case_id -> ranked figure_ids. ``gold``: case_id -> relevant
    figure_ids. Cases missing from ``results`` score 0 and are counted in
    ``cases_missing_results`` — silent exclusion would inflate the metric.
    """
    per_case: Dict[str, Dict[str, float]] = {}
    missing: List[str] = []
    for case_id, gold_ids in gold.items():
        ranked = results.get(case_id)
        if ranked is None:
            missing.append(case_id)
            ranked = []
        per_case[case_id] = {
            f"recall@{k}": recall_at_k(list(ranked), set(gold_ids), k) for k in ks
        }
    aggregate = {
        f"recall@{k}": round(
            sum(m[f"recall@{k}"] for m in per_case.values()) / len(per_case), 4
        )
        for k in ks
    } if per_case else {}
    return {
        "cases": len(per_case),
        "cases_missing_results": sorted(missing),
        "per_case": per_case,
        "aggregate": aggregate,
    }


def run_visual_eval(
    *,
    kb_path: Optional[str],
    results_path: Optional[str],
    gold_path: Optional[str],
    ks: Sequence[int] = DEFAULT_KS,
    out_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Real-data entry point. Deferred (explicitly, never with fake numbers)
    when the VLM-parsed KB or retrieval results are unavailable."""
    missing = [
        name
        for name, path in (("visuals_kb", kb_path), ("retrieval_results", results_path), ("gold", gold_path))
        if not path or not os.path.exists(path)
    ]
    if missing:
        report = {
            "status": "deferred",
            "reason": (
                f"required inputs unavailable: {missing}. Real visual evaluation "
                "is deferred until the VLM endpoint key is provisioned and "
                "02_visuals_kb.jsonl is produced. No metrics are reported "
                "rather than fabricated."
            ),
            "metrics": None,
        }
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
        return report

    with open(gold_path, "r", encoding="utf-8") as fh:  # type: ignore[arg-type]
        gold = {c["case_id"]: c["gold_figure_ids"] for c in
                (json.loads(l) for l in fh if l.strip())}
    with open(results_path, "r", encoding="utf-8") as fh:  # type: ignore[arg-type]
        results = {c["case_id"]: c["ranked_candidates"] for c in
                   (json.loads(l) for l in fh if l.strip())}
    report = {
        "status": "ok",
        "kb_path": kb_path,
        "metrics": evaluate_visual_retrieval(results, gold, ks),
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    return report
