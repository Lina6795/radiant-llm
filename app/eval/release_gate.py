"""Release Gate (M8).

Compares a candidate eval report against a baseline report and decides
pass/fail with per-rule detail:

1. ``core_recall`` -- core Recall metrics must not regress beyond a
   configurable threshold (default 0.0, i.e. no regression at all).
2. ``unsupported_rate`` -- unsupported-claim rate (HR) must not rise
   beyond a configurable threshold. Fail-closed: a metric measured in
   the baseline but unmeasured in the candidate fails the rule.
3. ``integrity`` -- isolation / idempotency / recovery tests must all
   pass: every durable (recovery/idempotency) and memory (workspace
   isolation) case, plus the context isolation/red-line cases. A skipped
   integrity layer means "cannot verify" -> fail.
4. ``judge_human_audit`` -- every measured judge-based metric must carry
   a human-audit consistency field (``human_review`` with an
   ``agreement`` entry). Judge metrics are never treated as fully
   objective; unmeasured judge metrics (``not_measured``) are fine.
5. ``layer_status`` -- no layer may newly fail relative to the baseline.

CLI:

    PYTHONPATH=app ./runtime/bin/python3.12 -m eval.release_gate \\
      --current artifacts/eval/<run>/report.json \\
      --baseline artifacts/eval/<baseline>/report.json [--config gate.json]

Exit code 0 on pass, 1 on fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RULE_PASS = "pass"
RULE_FAIL = "fail"
RULE_SKIP = "skip"


@dataclass
class GateConfig:
    """Gate thresholds. All configurable; defaults are fail-closed."""

    recall_metrics: List[str] = field(default_factory=lambda: [
        "retrieval.recall@20",
        "retrieval.anchor_hit@20",
        "retrieval.recall@5",
    ])
    recall_max_drop: float = 0.0
    unsupported_metrics: List[str] = field(default_factory=lambda: [
        "verification.hr",
    ])
    unsupported_max_increase: float = 0.0
    integrity_all_pass_layers: List[str] = field(default_factory=lambda: [
        "durable", "memory",
    ])
    integrity_required_cases: Dict[str, List[str]] = field(default_factory=lambda: {
        "context": ["CTX-T05", "CTX-T07", "CTX-T08"],
    })

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown gate config keys: {sorted(unknown)}")
        return cls(**data)


@dataclass
class RuleResult:
    rule: str
    status: str                    # pass | fail | skip
    detail: str
    metric: Optional[str] = None
    baseline: Any = None
    current: Any = None
    threshold: Any = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "status": self.status, "detail": self.detail,
            "metric": self.metric, "baseline": self.baseline,
            "current": self.current, "threshold": self.threshold,
        }


def _load_report(path: Path) -> Dict[str, Any]:
    path = path / "report.json" if path.is_dir() else path
    report = json.loads(path.read_text(encoding="utf-8"))
    if "schema_version" not in report or "layers" not in report:
        raise ValueError(f"{path} is not a radiant eval report")
    return report


def _cases(report: Dict[str, Any], layer: str) -> List[Dict[str, Any]]:
    return report["layers"].get(layer, {}).get("cases", [])


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _rule_core_recall(current: Dict[str, Any], baseline: Dict[str, Any],
                      config: GateConfig) -> List[RuleResult]:
    results = []
    cur, base = current.get("metrics_flat", {}), baseline.get("metrics_flat", {})
    for metric in config.recall_metrics:
        b, c = base.get(metric), cur.get(metric)
        if b is None and c is None:
            results.append(RuleResult(
                "core_recall", RULE_SKIP,
                f"{metric} not measured in either report", metric=metric))
        elif c is None:
            results.append(RuleResult(
                "core_recall", RULE_FAIL,
                f"{metric} measured in baseline ({b}) but missing in current; "
                f"cannot verify no-regression (fail-closed)",
                metric=metric, baseline=b, current=None))
        elif b is None:
            results.append(RuleResult(
                "core_recall", RULE_SKIP,
                f"{metric} has no baseline value; recorded for the next gate",
                metric=metric, current=c))
        else:
            drop = b - c
            ok = drop <= config.recall_max_drop
            results.append(RuleResult(
                "core_recall", RULE_PASS if ok else RULE_FAIL,
                f"{metric}: baseline {b} -> current {c} "
                f"(drop {round(drop, 6)}, allowed {config.recall_max_drop})",
                metric=metric, baseline=b, current=c,
                threshold=config.recall_max_drop))
    return results


def _rule_unsupported(current: Dict[str, Any], baseline: Dict[str, Any],
                      config: GateConfig) -> List[RuleResult]:
    results = []
    cur, base = current.get("metrics_flat", {}), baseline.get("metrics_flat", {})
    for metric in config.unsupported_metrics:
        b, c = base.get(metric), cur.get(metric)
        if b is None and c is None:
            results.append(RuleResult(
                "unsupported_rate", RULE_SKIP,
                f"{metric} not measured in either report (judge-based)",
                metric=metric))
        elif c is None:
            results.append(RuleResult(
                "unsupported_rate", RULE_FAIL,
                f"{metric} measured in baseline ({b}) but unmeasured in current; "
                f"refusing to release with less measurement (fail-closed)",
                metric=metric, baseline=b, current=None))
        elif b is None:
            results.append(RuleResult(
                "unsupported_rate", RULE_SKIP,
                f"{metric} has no baseline value; recorded for the next gate",
                metric=metric, current=c))
        else:
            rise = c - b
            ok = rise <= config.unsupported_max_increase
            results.append(RuleResult(
                "unsupported_rate", RULE_PASS if ok else RULE_FAIL,
                f"{metric}: baseline {b} -> current {c} "
                f"(rise {round(rise, 6)}, allowed {config.unsupported_max_increase})",
                metric=metric, baseline=b, current=c,
                threshold=config.unsupported_max_increase))
    return results


def _rule_integrity(current: Dict[str, Any], baseline: Dict[str, Any],
                    config: GateConfig) -> List[RuleResult]:
    del baseline  # integrity is absolute: the candidate alone must be green
    results = []
    for layer in config.integrity_all_pass_layers:
        layer_report = current["layers"].get(layer)
        if layer_report is None or layer_report["status"] == "skipped":
            results.append(RuleResult(
                "integrity", RULE_FAIL,
                f"layer {layer} skipped/missing in current report; "
                f"isolation/idempotency/recovery cannot be verified (fail-closed)"))
            continue
        failed = [c["case_id"] for c in _cases(current, layer)
                  if c["status"] == "fail"]
        skipped = [c["case_id"] for c in _cases(current, layer)
                   if c["status"] == "skip"]
        ok = not failed and not skipped
        results.append(RuleResult(
            "integrity", RULE_PASS if ok else RULE_FAIL,
            f"layer {layer}: {len(_cases(current, layer))} cases, "
            f"failed={failed or 'none'}, skipped={skipped or 'none'}"))
    for layer, case_ids in config.integrity_required_cases.items():
        cases = {c["case_id"]: c for c in _cases(current, layer)}
        for case_id in case_ids:
            case = cases.get(case_id)
            if case is None:
                results.append(RuleResult(
                    "integrity", RULE_FAIL,
                    f"required integrity case {layer}/{case_id} absent from report"))
            else:
                ok = case["status"] == "pass"
                results.append(RuleResult(
                    "integrity", RULE_PASS if ok else RULE_FAIL,
                    f"{layer}/{case_id}: {case['status']}"
                    + (f" ({case.get('error')})" if not ok and case.get("error") else ""),
                    baseline="pass", current=case["status"]))
    return results


def _rule_judge_human_audit(current: Dict[str, Any], baseline: Dict[str, Any],
                            config: GateConfig) -> List[RuleResult]:
    del baseline, config
    results = []
    found = False
    for layer, layer_report in current["layers"].items():
        for name, metric in layer_report.get("metrics", {}).items():
            if metric.get("kind") != "judge" or metric.get("status") != "measured":
                continue
            found = True
            review = metric.get("human_review") or {}
            ok = "agreement" in review
            judge = metric.get("judge") or {}
            results.append(RuleResult(
                "judge_human_audit", RULE_PASS if ok else RULE_FAIL,
                f"{layer}.{name}: judge={judge.get('judge_model')}, "
                f"human_review.agreement={review.get('agreement')!r}; "
                f"judge metrics are never fully objective and require a "
                f"human-audit consistency field",
                metric=f"{layer}.{name}"))
    if not found:
        results.append(RuleResult(
            "judge_human_audit", RULE_SKIP,
            "no measured judge-based metrics in current report"))
    return results


def _rule_layer_status(current: Dict[str, Any], baseline: Dict[str, Any],
                       config: GateConfig) -> List[RuleResult]:
    del config
    results = []
    for layer, layer_report in current["layers"].items():
        base_layer = baseline["layers"].get(layer, {})
        newly_failed = (layer_report["status"] == "failed"
                        and base_layer.get("status") != "failed")
        results.append(RuleResult(
            "layer_status", RULE_FAIL if newly_failed else RULE_PASS,
            f"layer {layer}: baseline {base_layer.get('status')!r} -> "
            f"current {layer_report['status']!r}"))
    return results


_RULES = (
    _rule_core_recall,
    _rule_unsupported,
    _rule_integrity,
    _rule_judge_human_audit,
    _rule_layer_status,
)


def evaluate_gate(
    current: Dict[str, Any],
    baseline: Dict[str, Any],
    config: Optional[GateConfig] = None,
) -> Dict[str, Any]:
    """Run every gate rule; overall pass iff no rule fails."""
    config = config or GateConfig()
    rules: List[RuleResult] = []
    for rule_fn in _RULES:
        rules.extend(rule_fn(current, baseline, config))
    verdict = "pass" if all(r.status != RULE_FAIL for r in rules) else "fail"
    return {
        "gate": verdict,
        "current_run_id": current.get("run_id"),
        "baseline_run_id": baseline.get("run_id"),
        "config": {
            "recall_metrics": config.recall_metrics,
            "recall_max_drop": config.recall_max_drop,
            "unsupported_metrics": config.unsupported_metrics,
            "unsupported_max_increase": config.unsupported_max_increase,
            "integrity_all_pass_layers": config.integrity_all_pass_layers,
            "integrity_required_cases": config.integrity_required_cases,
        },
        "rules": [r.to_dict() for r in rules],
        "n_fail": sum(1 for r in rules if r.status == RULE_FAIL),
        "n_pass": sum(1 for r in rules if r.status == RULE_PASS),
        "n_skip": sum(1 for r in rules if r.status == RULE_SKIP),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--current", type=Path, required=True,
                        help="candidate report.json (or run dir)")
    parser.add_argument("--baseline", type=Path, required=True,
                        help="baseline report.json (or run dir)")
    parser.add_argument("--config", type=Path, default=None,
                        help="optional JSON gate config (see GateConfig fields)")
    parser.add_argument("--out", type=Path, default=None,
                        help="optional path to write the gate result JSON")
    args = parser.parse_args(argv)

    current = _load_report(args.current)
    baseline = _load_report(args.baseline)
    config = GateConfig.from_dict(
        json.loads(args.config.read_text(encoding="utf-8"))) if args.config else None
    result = evaluate_gate(current, baseline, config)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["gate"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
