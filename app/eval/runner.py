"""Unified eval runner (M8).

Single command:

    cd /mnt/lina/radiant-llm && \\
      PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner \\
        --all --out artifacts/eval/<run_id>/

Options:

* ``--layer NAME`` (repeatable) -- run only the given layer(s);
* ``--baseline PATH`` -- path to a baseline ``report.json`` (or its run
  directory); the report gains a metric-by-metric comparison section;
* ``--list`` -- print the registry discovery table and exit.

Outputs under the run directory:

* ``report.json`` -- machine-readable report (schema
  ``radiant-eval-report/v1``): per-layer, per-dataset, per-case results;
  every case carries its config fingerprint and artifact/trace path, so
  every aggregate metric traces back to case_id + configuration + trace;
* ``report.md`` -- human-readable rendering of the same report;
* ``<layer>/<dataset>/<case_id>.json`` -- per-case artifacts/traces.

Missing datasets or layers without an offline entry point are recorded
as ``skipped`` with a reason -- never an error, never a silent pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # app/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from eval.adapters import (  # noqa: E402
    LAYER_EXECUTORS,
    DatasetResult,
    LayerContext,
    ensure_import_paths,
)
from eval.fingerprint import collect_fingerprint  # noqa: E402
from eval.registry import (  # noqa: E402
    LAYERS,
    DatasetSpec,
    discover,
    load_cases,
    specs_for_layer,
)

SCHEMA_VERSION = "radiant-eval-report/v1"
DEFAULT_OUT_ROOT = Path("artifacts/eval")


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _layer_metrics(datasets: Dict[str, DatasetResult]) -> Dict[str, dict]:
    """Merge per-dataset metrics into layer-level metrics.

    Numeric measured values are pooled across datasets (weighted by
    n_cases); the per-dataset breakdown stays under ``datasets``.
    """
    pooled: Dict[str, Dict[str, Any]] = {}
    for ds in datasets.values():
        for name, metric in ds.metrics.items():
            entry = pooled.setdefault(
                name, {"name": name, "kind": metric.kind, "values": [],
                       "status": "not_measured", "reason": "no_measured_cases"})
            if metric.status == "measured" and metric.value is not None:
                entry["values"].append((metric.value, max(1, metric.n_cases)))
            if metric.kind == "judge" and metric.judge:
                entry.setdefault("judges", []).append(metric.judge)
    out: Dict[str, dict] = {}
    for name, entry in pooled.items():
        values = entry["values"]
        result = {
            "name": name,
            "kind": entry["kind"],
            "value": (round(sum(v * n for v, n in values) / sum(n for _, n in values), 6)
                      if values else None),
            "status": "measured" if values else "not_measured",
            "n_cases": sum(n for _, n in values),
            "reason": None if values else entry["reason"],
            "judge": entry.get("judges", [None])[0] if entry.get("judges") else None,
            "human_review": None,
        }
        out[name] = result
    return out


def run_layers(
    layers: List[str],
    out_dir: Path,
    *,
    baseline_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ensure_import_paths()
    run_id = out_dir.name
    fingerprint = collect_fingerprint()
    fp_hash = fingerprint["fingerprint_hash"]
    discovery = discover()

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_fingerprint": fingerprint,
        "discovery": {
            "unregistered_files": discovery["unregistered_files"],
            "missing_datasets": sorted(
                name for name, d in discovery["datasets"].items()
                if d.status == "missing"),
        },
        "layers": {},
        "metrics_flat": {},
        "baseline_comparison": None,
    }

    for layer in layers:
        t0 = time.perf_counter()
        layer_report: Dict[str, Any] = {
            "status": "ok", "skip_reason": None, "datasets": {},
            "cases": [], "metrics": {}, "duration_s": None,
        }
        specs = specs_for_layer(layer)
        present: Dict[DatasetSpec, List[dict]] = {}
        for spec in specs:
            found = discovery["datasets"][spec.name]
            ds_info: Dict[str, Any] = {
                "path": str(spec.path.relative_to(spec.path.parents[1])),
                "status": found.status,
                "n_cases": found.n_cases,
                "executor": spec.executor,
                "metric_families": list(spec.metric_families),
                "baseline_results": spec.baseline_results,
            }
            if found.status == "missing":
                ds_info["skip_reason"] = found.reason
            else:
                present[spec] = load_cases(spec)
                data = fingerprint["data_versions"].get(spec.filename, {})
                ds_info["data_version_hash"] = data.get("sha256")
            layer_report["datasets"][spec.name] = ds_info

        executor = LAYER_EXECUTORS.get(layer)
        if executor is None:
            layer_report["status"] = "skipped"
            layer_report["skip_reason"] = f"no executor registered for layer {layer!r}"
        elif not present:
            layer_report["status"] = "skipped"
            layer_report["skip_reason"] = (
                "all datasets of this layer are missing: "
                + ", ".join(s.filename for s in specs))
        else:
            ctx = LayerContext(run_id=run_id, out_dir=out_dir / layer,
                               fingerprint_hash=fp_hash)
            try:
                results = executor(present, ctx)
            except Exception as exc:  # noqa: BLE001 - recorded, run continues
                layer_report["status"] = "failed"
                layer_report["skip_reason"] = (
                    f"executor raised: {type(exc).__name__}: {exc}")
                results = {}
            dataset_results: Dict[str, Any] = {}
            for name, ds in results.items():
                if ds.status == "skipped":
                    layer_report["datasets"][name]["status"] = "skipped"
                    layer_report["datasets"][name]["skip_reason"] = ds.skip_reason
                dataset_results[name] = ds
                for case in ds.cases:
                    layer_report["cases"].append(case.to_dict())
            if results and all(ds.status == "skipped" for ds in results.values()):
                layer_report["status"] = "skipped"
                layer_report["skip_reason"] = "; ".join(
                    sorted({ds.skip_reason or "skipped" for ds in results.values()}))
            layer_report["metrics"] = _layer_metrics(dataset_results)
            layer_report["datasets_metrics"] = {
                name: {m_name: m.to_dict() for m_name, m in ds.metrics.items()}
                for name, ds in dataset_results.items() if ds.status != "skipped"}
        layer_report["duration_s"] = round(time.perf_counter() - t0, 2)
        report["layers"][layer] = layer_report
        for m_name, metric in layer_report["metrics"].items():
            if metric["status"] == "measured" and metric["value"] is not None:
                report["metrics_flat"][f"{layer}.{m_name}"] = metric["value"]
        status = layer_report["status"]
        n_pass = sum(1 for c in layer_report["cases"] if c["status"] == "pass")
        n_review = sum(1 for c in layer_report["cases"] if c["status"] == "review")
        print(f"[eval] {layer}: {status} "
              f"({n_pass}/{len(layer_report['cases'])} cases pass"
              f"{f', {n_review} review' if n_review else ''}, "
              f"{layer_report['duration_s']}s)", flush=True)

    report["summary"] = _summary(report)
    if baseline_path:
        report["baseline_comparison"] = compare_to_baseline(report, baseline_path)
    return report


def _summary(report: Dict[str, Any]) -> Dict[str, Any]:
    cases = [c for layer in report["layers"].values() for c in layer["cases"]]
    return {
        "layers_ok": sum(1 for l in report["layers"].values() if l["status"] == "ok"),
        "layers_skipped": sum(1 for l in report["layers"].values() if l["status"] == "skipped"),
        "layers_failed": sum(1 for l in report["layers"].values() if l["status"] == "failed"),
        "cases_total": len(cases),
        "cases_passed": sum(1 for c in cases if c["status"] == "pass"),
        "cases_failed": sum(1 for c in cases if c["status"] == "fail"),
        "cases_skipped": sum(1 for c in cases if c["status"] == "skip"),
        "cases_review": sum(1 for c in cases if c["status"] == "review"),
        "duration_s": round(sum(l["duration_s"] or 0 for l in report["layers"].values()), 2),
    }


def compare_to_baseline(report: Dict[str, Any], baseline_path: Path) -> Dict[str, Any]:
    path = baseline_path / "report.json" if baseline_path.is_dir() else baseline_path
    baseline = json.loads(path.read_text(encoding="utf-8"))
    base_flat = baseline.get("metrics_flat", {})
    cur_flat = report.get("metrics_flat", {})
    deltas: Dict[str, Any] = {}
    for key in sorted(set(base_flat) | set(cur_flat)):
        b, c = base_flat.get(key), cur_flat.get(key)
        deltas[key] = {
            "baseline": b, "current": c,
            "delta": round(c - b, 6) if b is not None and c is not None else None,
        }
    return {
        "baseline_run_id": baseline.get("run_id"),
        "baseline_path": str(path),
        "metrics": deltas,
        "missing_in_current": sorted(k for k in base_flat if k not in cur_flat),
        "new_in_current": sorted(k for k in cur_flat if k not in base_flat),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    s = report["summary"]
    fp = report["config_fingerprint"]
    lines.append(f"# Eval report `{report['run_id']}`")
    lines.append("")
    lines.append(f"- created: {report['created_at']}")
    lines.append(f"- git: `{fp['git']['commit']}` (dirty: {fp['git']['dirty']})")
    lines.append(f"- fingerprint: `{fp['fingerprint_hash']}`")
    lines.append(f"- layers ok/skipped/failed: "
                 f"{s['layers_ok']}/{s['layers_skipped']}/{s['layers_failed']}")
    lines.append(f"- cases pass/fail/skip/review: "
                 f"{s['cases_passed']}/{s['cases_failed']}/{s['cases_skipped']}"
                 f"/{s.get('cases_review', 0)} "
                 f"of {s['cases_total']} in {s['duration_s']}s")
    if report["discovery"]["missing_datasets"]:
        lines.append(f"- missing datasets (skipped): "
                     f"{', '.join(report['discovery']['missing_datasets'])}")
    if report["discovery"]["unregistered_files"]:
        lines.append(f"- WARNING unregistered benchmark files: "
                     f"{', '.join(report['discovery']['unregistered_files'])}")
    lines.append("")

    for layer, lr in report["layers"].items():
        lines.append(f"## {layer} — {lr['status']} ({lr['duration_s']}s)")
        lines.append("")
        if lr["skip_reason"]:
            lines.append(f"> {lr['skip_reason']}")
            lines.append("")
        if lr["metrics"]:
            lines.append("| metric | value | status | kind | n |")
            lines.append("|---|---|---|---|---|")
            for name, m in sorted(lr["metrics"].items()):
                value = m["value"] if m["value"] is not None else "—"
                lines.append(f"| {name} | {value} | {m['status']} | {m['kind']} "
                             f"| {m['n_cases']} |")
            lines.append("")
        if lr["cases"]:
            lines.append("| case | dataset | status | latency ms | artifact |")
            lines.append("|---|---|---|---|---|")
            for c in lr["cases"]:
                artifact = c.get("artifact_uri") or c.get("trace_uri") or "—"
                if artifact != "—":
                    artifact = f"`{artifact}`"
                lat = c.get("latency_ms") if c.get("latency_ms") is not None else "—"
                status = c["status"] + (f" ({c['error'][:80]})"
                                        if c["status"] != "pass" and c.get("error")
                                        else "")
                lines.append(f"| {c['case_id']} | {c['dataset']} | {status} "
                             f"| {lat} | {artifact} |")
            lines.append("")

    comparison = report.get("baseline_comparison")
    if comparison:
        lines.append(f"## Baseline comparison vs `{comparison['baseline_run_id']}`")
        lines.append("")
        lines.append("| metric | baseline | current | delta |")
        lines.append("|---|---|---|---|")
        for key, d in comparison["metrics"].items():
            b = d["baseline"] if d["baseline"] is not None else "—"
            c = d["current"] if d["current"] is not None else "—"
            delta = d["delta"] if d["delta"] is not None else "—"
            lines.append(f"| {key} | {b} | {c} | {delta} |")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="run every registered layer")
    parser.add_argument("--layer", action="append", default=[],
                        choices=list(LAYERS), help="run only this layer (repeatable)")
    parser.add_argument("--out", type=Path, default=None,
                        help="run output directory (default artifacts/eval/<run_id>)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="baseline report.json (or run dir) for comparison")
    parser.add_argument("--list", action="store_true",
                        help="print registry discovery and exit")
    args = parser.parse_args(argv)

    if args.list:
        discovery = discover()
        for name, d in sorted(discovery["datasets"].items()):
            print(f"{name:20s} layer={d.spec.layer:13s} {d.status:8s} "
                  f"n={d.n_cases:<3d} executor={d.spec.executor}")
        if discovery["unregistered_files"]:
            print("UNREGISTERED:", ", ".join(discovery["unregistered_files"]))
        return 0

    layers = list(LAYERS) if args.all else list(dict.fromkeys(args.layer))
    if not layers:
        parser.error("pass --all or at least one --layer")

    if args.out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        args.out = DEFAULT_OUT_ROOT / f"m8-{stamp}"
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    report = run_layers(layers, out_dir, baseline_path=args.baseline)
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    s = report["summary"]
    print(f"[eval] wrote {report_path} and {out_dir / 'report.md'}", flush=True)
    print(f"[eval] cases pass/fail/skip: "
          f"{s['cases_passed']}/{s['cases_failed']}/{s['cases_skipped']}", flush=True)
    return 0 if s["layers_failed"] == 0 and s["cases_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
