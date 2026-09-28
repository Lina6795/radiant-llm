"""Dataset registry and discovery for the eval harness.

Each frozen dataset declares: its path, the owning layer, the executor
adapter that runs it, the metric families it feeds, and where its
baseline result file lives. Discovery scans ``benchmarks/*.jsonl`` and
matches files against the registry:

* registered + file present -> runnable;
* registered + file missing -> ``skipped`` with a reason (e.g. the M7
  ``answer_cases.jsonl`` / ``visual_cases.jsonl`` not landed yet);
* file present but unregistered -> reported as ``unregistered`` so new
  datasets cannot silently evade the harness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"


@dataclass(frozen=True)
class DatasetSpec:
    name: str                      # dataset name, e.g. "router_cases"
    filename: str                  # file under benchmarks/
    layer: str                     # owning layer, e.g. "control"
    executor: str                  # adapter key in app.eval.adapters.EXECUTORS
    metric_families: tuple = ("deterministic",)
    baseline_results: Optional[str] = None  # repo-relative path to baseline artifact
    notes: str = ""

    @property
    def path(self) -> Path:
        return BENCHMARKS_DIR / self.filename


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        name="teaching_case",
        filename="teaching_case.jsonl",
        layer="agent_e2e",
        executor="agent.chain",
        metric_families=("deterministic", "operational", "judge"),
        notes="S8: real Agent main chain E2E (TEACH-T01).",
    ),
    DatasetSpec(
        name="visual_gold_cases",
        filename="visual_gold_cases.jsonl",
        layer="agent_e2e",
        executor="agent.chain",
        metric_families=("deterministic", "operational"),
        notes="S7/S8: real visual gold cases through the main chain.",
    ),
    DatasetSpec(
        name="router_cases",
        filename="router_cases.jsonl",
        layer="control",
        executor="control.router",
        metric_families=("deterministic",),
    ),
    DatasetSpec(
        name="policy_cases",
        filename="policy_cases.jsonl",
        layer="control",
        executor="control.policy",
        metric_families=("deterministic",),
    ),
    DatasetSpec(
        name="runtime_cases",
        filename="runtime_cases.jsonl",
        layer="durable",
        executor="durable.scenarios",
        metric_families=("deterministic",),
    ),
    DatasetSpec(
        name="retrieval_cases",
        filename="retrieval_cases.jsonl",
        layer="retrieval",
        executor="retrieval.pipeline",
        metric_families=("deterministic", "operational"),
        baseline_results="artifacts/retrieval/m4-20260922/metrics.json",
    ),
    DatasetSpec(
        name="baseline_cases",
        filename="baseline_cases.jsonl",
        layer="retrieval",
        executor="retrieval.pipeline",
        metric_families=("deterministic", "operational"),
        baseline_results="artifacts/retrieval/m4-20260922/metrics.json",
        notes="M0 frozen baseline questions; same pipeline as retrieval_cases.",
    ),
    DatasetSpec(
        name="context_cases",
        filename="context_cases.jsonl",
        layer="context",
        executor="context.cases",
        metric_families=("deterministic",),
        baseline_results="artifacts/context/m5-20260922/metrics.json",
    ),
    DatasetSpec(
        name="memory_cases",
        filename="memory_cases.jsonl",
        layer="memory",
        executor="memory.gates",
        metric_families=("deterministic",),
    ),
    DatasetSpec(
        name="answer_cases",
        filename="answer_cases.jsonl",
        layer="verification",
        executor="verification.answer",
        metric_families=("deterministic", "judge", "human_audit"),
        baseline_results="artifacts/verification/m7-20260922/summary.json",
        notes="M7 B2 arm (planner+tools+verifier) via app.verification.experiment; "
              "B0 HTTP arm excluded (service-internal retrieval, not observable).",
    ),
    DatasetSpec(
        name="visual_cases",
        filename="visual_cases.jsonl",
        layer="verification",
        executor="verification.visual",
        metric_families=("deterministic", "judge"),
        baseline_results="artifacts/verification/m7-20260922/summary.json",
        notes="M7 synthetic fixtures through visual_qa / visual_eval; feeds ViR.",
    ),
)

_BY_NAME: Dict[str, DatasetSpec] = {spec.name: spec for spec in DATASETS}
_BY_FILE: Dict[str, DatasetSpec] = {spec.filename: spec for spec in DATASETS}
LAYERS: tuple[str, ...] = tuple(dict.fromkeys(spec.layer for spec in DATASETS))


def get_spec(name: str) -> DatasetSpec:
    return _BY_NAME[name]


def specs_for_layer(layer: str) -> List[DatasetSpec]:
    return [spec for spec in DATASETS if spec.layer == layer]


def load_cases(spec: DatasetSpec) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in spec.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@dataclass
class DiscoveredDataset:
    spec: DatasetSpec
    status: str                    # "present" | "missing"
    n_cases: int = 0
    reason: Optional[str] = None


def discover(benchmarks_dir: Path = BENCHMARKS_DIR) -> Dict[str, Any]:
    """Scan ``benchmarks/*.jsonl`` and match against the registry."""
    found: Dict[str, DiscoveredDataset] = {}
    for spec in DATASETS:
        path = benchmarks_dir / spec.filename
        if path.is_file():
            n = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            found[spec.name] = DiscoveredDataset(spec=spec, status="present", n_cases=n)
        else:
            found[spec.name] = DiscoveredDataset(
                spec=spec,
                status="missing",
                reason=f"dataset file {spec.filename} not found under {benchmarks_dir}",
            )
    registered_files = set(_BY_FILE)
    unregistered = sorted(
        p.name for p in benchmarks_dir.glob("*.jsonl") if p.name not in registered_files
    ) if benchmarks_dir.is_dir() else []
    return {"datasets": found, "unregistered_files": unregistered}
