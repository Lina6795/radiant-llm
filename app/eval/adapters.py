"""Layer executor adapters for the eval runner.

One executor per owning layer. Each executor receives the layer's
present datasets (frozen cases already loaded) plus a :class:`LayerContext`
and returns per-dataset :class:`DatasetResult` objects with per-case
pass/fail, metrics and artifact paths.

Rules every executor follows:

* Prefer calling library functions directly (offline). Layers with no
  offline entry point are skipped with a recorded reason -- never an
  error, never a silent pass.
* Missing optional dependencies (vector store, evidence DB, the M7
  ``app.verification`` package) skip the affected dataset/cases with a
  reason.
* Every case result carries the layer config fingerprint and, where the
  layer produces traces, a trace/artifact path, so any aggregate metric
  can be traced back to case_id + configuration + trace.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .metrics import MetricResult
from .registry import DatasetSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

DEFAULT_EVIDENCE_DB = "artifacts/baseline/m0-20260922/evidence.db"
DEFAULT_VECTOR_STORE = "artifacts/baseline/m0-20260922/output/local_vector_store"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_REVIEW = "review"


def ensure_import_paths() -> None:
    """Make both ``app.*`` and top-level layer packages importable."""
    for path in (str(REPO_ROOT), str(APP_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)


@dataclass
class CaseResult:
    case_id: str
    dataset: str
    status: str                       # pass | fail | skip | review
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None       # failure/skip detail
    artifact_uri: Optional[str] = None
    trace_uri: Optional[str] = None
    latency_ms: Optional[float] = None
    config_fingerprint: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetResult:
    dataset: str
    status: str                       # ok | skipped | failed
    cases: List[CaseResult] = field(default_factory=list)
    metrics: Dict[str, MetricResult] = field(default_factory=dict)
    skip_reason: Optional[str] = None


@dataclass
class LayerContext:
    run_id: str
    out_dir: Path                     # layer-scoped output directory
    fingerprint_hash: str


def _metric(name: str, values: List[Optional[float]], *,
            kind: str = "deterministic") -> MetricResult:
    measured = [v for v in values if v is not None]
    return MetricResult(
        name=name,
        value=round(sum(measured) / len(measured), 6) if measured else None,
        status="measured" if measured else "not_measured",
        kind=kind,
        n_cases=len(values),
        reason=None if measured else "no_measured_cases",
    )


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return str(path)


def _code_matches(actual_codes: List[str], expected: str) -> bool:
    return any(
        code == expected or code.startswith(expected + ":") for code in actual_codes
    )


# ---------------------------------------------------------------------------
# control layer: router_cases + policy_cases (deterministic, offline)
# ---------------------------------------------------------------------------

def run_control_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    from app.control.budget import BudgetLedger
    from app.control.models import (
        Action,
        Intent,
        ReasonCode,
        ResponseContract,
        RouterDecision,
        RunStatus,
    )
    from app.control.router import RuleRouter
    from app.control.scheduler import ControlPlane

    results: Dict[str, DatasetResult] = {}
    allowances = {"default": 100_000, "readonly": 100_000,
                  "lowbudget": 1_000, "full": 100_000}

    for spec, cases in datasets.items():
        ds = DatasetResult(dataset=spec.name, status="ok")
        router = RuleRouter()
        for case in cases:
            t0 = time.perf_counter()
            failures: List[str] = []
            expected = case["expected"]
            detail: Dict[str, Any] = {}
            try:
                if spec.executor == "control.router":
                    decision = router(case["input"]["query"])
                    detail = {
                        "action": decision.action.value,
                        "intent": decision.intent.value,
                        "reason_codes": list(decision.reason_codes),
                    }
                    if decision.action.value != expected["action"]:
                        failures.append(
                            f"action: expected {expected['action']}, got {detail['action']}")
                    if "intent" in expected and decision.intent.value != expected["intent"]:
                        failures.append(
                            f"intent: expected {expected['intent']}, got {detail['intent']}")
                    for code in expected.get("reason_codes_contains", []):
                        if not _code_matches(decision.reason_codes, code):
                            failures.append(f"missing reason code {code}")
                    contract = expected.get("response_contract") or {}
                    if contract:
                        rc = decision.response_contract
                        if "format" in contract and rc.format.value != contract["format"]:
                            failures.append("response_contract.format mismatch")
                        if ("citation_required" in contract
                                and rc.citation_required != contract["citation_required"]):
                            failures.append("response_contract.citation_required mismatch")
                    if not decision.reason_codes:
                        failures.append("no stable reason code on decision")
                else:  # control.policy
                    def _tool_call_decision(_goal: str) -> RouterDecision:
                        return RouterDecision(
                            intent=Intent.KNOWLEDGE_QA,
                            action=Action.TOOL_CALL,
                            confidence=1.0,
                            reason_codes=[ReasonCode.ROUTER_KEYWORD_MATCH.value],
                            response_contract=ResponseContract(),
                        )

                    plane = ControlPlane.build(
                        router=_tool_call_decision,
                        planner=lambda _decision, _goal: case["input"]["plan"],
                        ledger=BudgetLedger(allowances=dict(allowances)),
                    )
                    summary = plane.run(case["input"]["goal"],
                                        workspace=case["input"]["workspace"])
                    detail = {
                        "status": summary.status.value,
                        "reason_codes": list(summary.reason_codes),
                        "tools_executed": summary.tools_executed,
                        "ledger_effects": plane.registry.ledger.effect_count,
                    }
                    if summary.status != RunStatus(expected["status"]):
                        failures.append(
                            f"status: expected {expected['status']}, got {detail['status']}")
                    for code in expected.get("reason_codes_contains", []):
                        if not _code_matches(summary.reason_codes, code):
                            failures.append(f"missing reason code {code}")
                    if summary.tools_executed != expected["tools_executed"]:
                        failures.append(
                            f"tools_executed: expected {expected['tools_executed']}, "
                            f"got {detail['tools_executed']}")
                    if expected["tools_executed"] == 0 and plane.registry.ledger.effect_count != 0:
                        failures.append("unauthorized tool side effect recorded")
                    if summary.status in (RunStatus.REJECTED, RunStatus.NEEDS_REVIEW) \
                            and not summary.reason_codes:
                        failures.append("reject/review without stable reason code")
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                failures.append(f"executor error: {type(exc).__name__}: {exc}")

            latency = (time.perf_counter() - t0) * 1000
            artifact = _write_json(
                ctx.out_dir / spec.name / f"{case['case_id']}.json",
                {"case": case, "detail": detail, "failures": failures})
            ds.cases.append(CaseResult(
                case_id=case["case_id"], dataset=spec.name,
                status=STATUS_FAIL if failures else STATUS_PASS,
                metrics=detail,
                error="; ".join(failures) if failures else None,
                artifact_uri=artifact, latency_ms=round(latency, 2),
                config_fingerprint=ctx.fingerprint_hash))
        passed = [c.status == STATUS_PASS for c in ds.cases]
        ds.metrics["pass_rate"] = _metric("pass_rate", [float(p) for p in passed])
        ds.metrics["accuracy"] = ds.metrics["pass_rate"]
        results[spec.name] = ds
    return results


# ---------------------------------------------------------------------------
# durable layer: runtime_cases (fault-injection scenario drivers from M3)
# ---------------------------------------------------------------------------

def run_durable_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    ensure_import_paths()
    try:
        from tests.durable.scenarios import run_case  # type: ignore
    except Exception as exc:  # noqa: BLE001
        reason = (f"durable scenario drivers unavailable "
                  f"(tests.durable.scenarios import failed: {exc})")
        return {spec.name: DatasetResult(dataset=spec.name, status="skipped",
                                         skip_reason=reason)
                for spec in datasets}

    results: Dict[str, DatasetResult] = {}
    for spec, cases in datasets.items():
        ds = DatasetResult(dataset=spec.name, status="ok")
        for case in cases:
            t0 = time.perf_counter()
            work_dir = ctx.out_dir / spec.name / case["case_id"]
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                outcome = run_case(case, work_dir)
                artifact = _write_json(
                    ctx.out_dir / spec.name / f"{case['case_id']}.json", outcome)
                ds.cases.append(CaseResult(
                    case_id=case["case_id"], dataset=spec.name,
                    status=STATUS_PASS if outcome["recovered"] else STATUS_FAIL,
                    metrics={"scenario": outcome["scenario"], **outcome["detail"]},
                    error=None if outcome["recovered"]
                    else json.dumps(outcome["mismatches"], ensure_ascii=False),
                    artifact_uri=artifact,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    config_fingerprint=ctx.fingerprint_hash))
            except Exception as exc:  # noqa: BLE001
                ds.cases.append(CaseResult(
                    case_id=case["case_id"], dataset=spec.name, status=STATUS_FAIL,
                    error=f"scenario raised: {type(exc).__name__}: {exc}",
                    latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    config_fingerprint=ctx.fingerprint_hash))
        passed = [c.status == STATUS_PASS for c in ds.cases]
        ds.metrics["pass_rate"] = _metric("pass_rate", [float(p) for p in passed])
        ds.metrics["recovery_rate"] = ds.metrics["pass_rate"]
        results[spec.name] = ds
    return results


# ---------------------------------------------------------------------------
# retrieval layer: retrieval_cases + baseline_cases (BM25 + dense pipeline)
# ---------------------------------------------------------------------------

def _retrieval_paths() -> tuple[Optional[Path], Optional[Path], Optional[str]]:
    db = Path(os.getenv("RADIANT_EVIDENCE_DB") or DEFAULT_EVIDENCE_DB)
    vs = Path(os.getenv("RADIANT_VECTOR_STORE") or DEFAULT_VECTOR_STORE)
    if not db.is_file():
        return None, None, f"evidence DB not found at {db} (set RADIANT_EVIDENCE_DB)"
    if not vs.is_dir():
        return None, None, f"vector store not found at {vs} (set RADIANT_VECTOR_STORE)"
    return db, vs, None


def run_retrieval_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    ensure_import_paths()
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    db, vs, skip_reason = _retrieval_paths()
    if skip_reason:
        return {spec.name: DatasetResult(dataset=spec.name, status="skipped",
                                         skip_reason=skip_reason)
                for spec in datasets}
    try:
        from evidence.store import get_evidence_store
        from retrieval.bm25 import BM25Index, load_text_evidence
        from retrieval.dense import DenseRetriever
        from retrieval.experiment import (
            aggregate,
            build_docname_map,
            evaluate_case,
            relevant_set,
        )
        from retrieval.pipeline import RetrievalPipeline, preset_configs
    except Exception as exc:  # noqa: BLE001
        reason = f"retrieval stack unavailable: {type(exc).__name__}: {exc}"
        return {spec.name: DatasetResult(dataset=spec.name, status="skipped",
                                         skip_reason=reason)
                for spec in datasets}

    store = get_evidence_store(str(db))
    items = load_text_evidence(store)
    bm25_index = BM25Index().build(items)
    dense = DenseRetriever(persist_directory=str(vs), evidence_items=items)
    dense._ensure_store()  # load embedding model once, outside case timing
    if not [c for c in dense.query("attention mechanism", k=5)
            if not c.evidence_id.startswith("unmapped:")]:
        return {spec.name: DatasetResult(
                    dataset=spec.name, status="skipped",
                    skip_reason="dense probe returned no mappable hits; "
                                "refusing to measure against a broken vector store")
                for spec in datasets}

    docname_map = build_docname_map(items)
    config = preset_configs()["A4_gates"]  # production default configuration
    pipeline = RetrievalPipeline(config, bm25_index=bm25_index,
                                 dense_retriever=dense, evidence_items=items)
    cfg_fp = config.fingerprint()

    results: Dict[str, DatasetResult] = {}
    for spec, cases in datasets.items():
        ds = DatasetResult(dataset=spec.name, status="ok")
        per_case: List[Dict[str, Any]] = []
        for case in cases:
            anchor = case.get("gold_anchor")
            if anchor and anchor.get("document") in docname_map:
                anchor = {**anchor, "document_id": docname_map[anchor["document"]]}
            t0 = time.perf_counter()
            trace = pipeline.run(case["question"], anchor=anchor)
            latency = (time.perf_counter() - t0) * 1000
            rel = relevant_set(case, items, docname_map)
            ev = evaluate_case(trace, rel)
            ev["case_id"] = case["case_id"]
            per_case.append(ev)
            trace_path = _write_json(
                ctx.out_dir / spec.name / f"{case['case_id']}.json",
                {"case": case, "metrics": ev, "trace": trace})
            hit = ev.get("anchor_hit@20")
            ds.cases.append(CaseResult(
                case_id=case["case_id"], dataset=spec.name,
                status=(STATUS_PASS if hit == 1.0
                        else STATUS_FAIL if hit is not None else STATUS_SKIP),
                metrics={k: v for k, v in ev.items() if k != "case_id"},
                error=None if hit == 1.0 else (
                    "gold anchor not in top-20" if hit is not None
                    else "case has no resolvable gold anchor"),
                trace_uri=trace_path, latency_ms=round(latency, 2),
                config_fingerprint=cfg_fp))
        agg = aggregate(per_case)
        for key in ("recall@5", "recall@20", "anchor_hit@5", "anchor_hit@20",
                    "mrr", "ndcg@20"):
            ds.metrics[key] = MetricResult(
                name=key, value=agg.get(key),
                status="measured" if agg.get(key) is not None else "not_measured",
                kind="deterministic", n_cases=agg.get("n_anchored_cases", 0),
                reason=None if agg.get(key) is not None else "no_anchored_cases")
        for key in ("latency_p50_ms", "latency_p95_ms"):
            ds.metrics[key] = MetricResult(
                name=key, value=agg.get(key),
                status="measured" if agg.get(key) is not None else "not_measured",
                kind="operational", n_cases=agg.get("n_cases", 0))
        results[spec.name] = ds
    return results


# ---------------------------------------------------------------------------
# context layer: context_cases (engine / compressor / isolate / budget drivers)
# ---------------------------------------------------------------------------

def _ctx_budget() -> "Any":
    from app.context.budgets import BudgetConfig

    return BudgetConfig(
        total_tokens=8192, response_reserve=1024,
        quotas={"system": 512, "active_turn": 512, "memory": 1024,
                "evidence": 4096, "artifact": 512, "tool_result": 512})


def _evidence_item(eid: str, text: str, *, pinned: bool = False,
                   page: int = 1, score: float = 0.5) -> "Any":
    from app.context.selector import EvidenceItem

    return EvidenceItem(evidence_id=eid, content=text, page=page,
                        score=score, pinned=pinned)


def _ctx_branch_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T01..T05: engine decision branches."""
    from app.context.engine import ContextEngine

    engine = ContextEngine(config=_ctx_budget())
    quotas = engine.config.quotas
    inputs = case["inputs"]
    filler = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do "

    system = "You are RADIANT-LLM. Answer with citations."
    active_turn = "Summarise the retrieved evidence."
    memory: Any = "short note"
    evidence: List[Any] = []
    tool_results: List[str] = []

    if inputs.get("memory_tokens") == "3x quota":
        memory = filler * (3 * quotas["memory"] // 6)
    if inputs.get("active_turn_tokens") == "5x quota":
        active_turn = filler * (5 * quotas["active_turn"] // 6)
    n_items = int(inputs.get("evidence_items", 0))
    for i in range(n_items):
        evidence.append(_evidence_item(
            f"ev-{i}", f"relevant fact {i}: the transformer uses attention.",
            pinned=i < int(inputs.get("pinned", 0)), page=i + 1,
            score=0.9 - i * 0.05))
    if inputs.get("pinned_tokens") == "2x evidence quota":
        evidence = [_evidence_item("ev-pinned", filler * (2 * quotas["evidence"] // 6),
                                   pinned=True, score=0.99)]

    result = engine.assemble(system=system, active_turn=active_turn,
                             memory=memory, evidence=evidence,
                             tool_results=tool_results)
    d = result.decision
    detail: Dict[str, Any] = {
        "decision": d.decision, "reasons": list(d.reasons),
        "n_selected": len(result.evidence), "n_drops": len(d.drops),
        "n_compressions": len(d.compressions),
        "drops": [dr.to_dict() for dr in d.drops],
    }
    checks: Dict[str, bool] = {"decision": d.decision == case["expected"]["decision"]}
    if "gold_retained" in case["expected"]:
        retained = len(result.evidence) / max(1, n_items)
        detail["gold_retained"] = retained
        checks["gold_retained"] = retained == case["expected"]["gold_retained"]
    if case["expected"].get("compression_lineage"):
        checks["compression_lineage"] = any(
            c.input_hash and c.output_hash and c.method for c in d.compressions)
    if case["expected"].get("turn_withheld"):
        checks["turn_withheld"] = active_turn not in result.text and any(
            dr.partition == "active_turn" for dr in d.drops)
    if case["expected"].get("no_silent_truncation"):
        checks["no_silent_truncation"] = all(
            dr.reason and dr.tokens > 0 for dr in d.drops)
    if "pin_dropped" in case["expected"]:
        pin_dropped = any(
            dr.item_id == "ev-pinned" and dr.reason != "pinned_evidence_exceeds_quota"
            for dr in d.drops)
        checks["pin_dropped"] = pin_dropped == case["expected"]["pin_dropped"]
    detail["checks"] = checks
    detail["passed"] = all(checks.values())
    return detail


def _ctx_anchor_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T06: pin on/off gold retention under pseudo-source pressure."""
    db, _, skip = _retrieval_paths()
    if skip:
        return {"passed": None, "skip_reason": f"needs evidence DB: {skip}"}
    ensure_import_paths()
    from context.experiment import (  # type: ignore
        build_candidates,
        experiment_budget,
        gold_ids,
        load_real_items,
        run_once,
    )
    from app.context.engine import ContextEngine, EngineOptions
    from app.context.isolate import ArtifactStore
    from app.context.tokenizer import TokenCounter

    inputs = case["inputs"]
    n_sources, reps, seed = inputs["sources"], inputs["reps"], inputs["seed"]
    counter = TokenCounter()
    items = load_real_items(str(db))
    gold = gold_ids(items)
    engine = ContextEngine(
        config=experiment_budget(), counter=counter,
        artifact_store=ArtifactStore(root=ctx.out_dir / "artifact_store",
                                     counter=counter),
        options=EngineOptions(isolate_threshold_tokens=1024))
    detail: Dict[str, Any] = {"passed": True}
    for pin in (True, False):
        retained = []
        for rep in range(reps):
            candidates = build_candidates(items, n_sources, rep, pin, seed, gold)
            retained.append(run_once(engine, counter, candidates, gold)
                            ["gold_chunks_retained"])
        key = "pin_on" if pin else "pin_off"
        detail[f"{key}_gold_retained"] = {
            "mean": round(statistics.fmean(retained), 4), "runs": retained}
    expected = case["expected"]
    if "pin_on_gold_retained" in expected:
        detail["passed"] = bool(
            detail["pin_on_gold_retained"]["mean"] == expected["pin_on_gold_retained"])
    # pin_off degradation is tolerated but always reported (never hidden).
    return detail


def _ctx_isolation_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T07: oversized tool result is spilled to disk, pointer in context."""
    from app.context.engine import ContextEngine, EngineOptions
    from app.context.isolate import ArtifactStore

    store = ArtifactStore(root=ctx.out_dir / "artifact_store")
    engine = ContextEngine(config=_ctx_budget(), artifact_store=store,
                           options=EngineOptions(isolate_threshold_tokens=64))
    big = "row,data,value\n" + "\n".join(f"r{i},d{i},{i}" for i in range(500))
    result = engine.assemble(system="sys", active_turn="q",
                             evidence=[_evidence_item("ev-0", "fact")],
                             tool_results=[big])
    ptr = result.decision.pointers[0] if result.decision.pointers else None
    checks = {
        "artifact_on_disk": ptr is not None
        and Path(ptr.uri[len("file://"):]).is_file(),
        "pointer_in_context": ptr is not None and ptr.stub() in result.text,
        "resolve_round_trip": ptr is not None and store.resolve(ptr) == big,
        "hash_verified": ptr is not None and store.resolve(ptr, verify=True) == big,
    }
    return {"checks": checks, "passed": all(checks.values()),
            "pointer": ptr.to_dict() if ptr else None}


def _ctx_compression_redline_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T08: the compressor refuses the evidence partition, always."""
    from app.context.compressor import Compressor, EvidenceCompressionError

    original = "evidence text that must never be rewritten"
    try:
        Compressor().compress_partition("evidence", original, 10)
        return {"checks": {"error": False}, "passed": False,
                "detail": "no EvidenceCompressionError raised"}
    except EvidenceCompressionError:
        return {"checks": {"error": True, "evidence_rewritten": False},
                "passed": True}


def _ctx_compression_lineage_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T09: rule+LLM recursive compression carries full lineage."""
    from app.context.compressor import Compressor

    def stub_summarizer(text: str, target: int) -> str:
        return " ".join(text.split()[: max(1, target)])

    # A very long first sentence makes the rule-level first+last skeleton
    # exceed the 1/8 target, forcing the recursive-LLM level to engage.
    long_sentence = " ".join(f"detail{i}" for i in range(300)) + "."
    text = long_sentence + " " + ("Short memory note sentence here. " * 40).strip()
    target = max(1, len(text.split()) // 8)
    compressor = Compressor(llm_summarizer=stub_summarizer,
                            model_name="stub-extractive-v1")
    result = compressor.compress_partition("memory", text, target)
    lineage = result.lineage.to_dict()
    required = set(case["expected"]["lineage_fields"])
    checks = {
        "lineage_fields": required <= set(lineage)
        and all(lineage[f] is not None for f in required),
        "method": lineage["method"] == case["expected"]["method"],
    }
    return {"checks": checks, "passed": all(checks.values()), "lineage": lineage}


def _ctx_budget_model_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T10: over-allocated quota sums are rejected at config time."""
    from app.context.budgets import PARTITIONS, BudgetConfig

    try:
        BudgetConfig(total_tokens=1024, response_reserve=0,
                     quotas={p: 1024 for p in PARTITIONS})
        return {"checks": {"config_rejected": False}, "passed": False}
    except ValueError:
        return {"checks": {"config_rejected": True}, "passed": True}


def _ctx_tokenizer_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T11: heuristic fallback vs tiktoken on English technical prose."""
    try:
        import tiktoken  # noqa: F401
    except Exception:
        return {"passed": None, "skip_reason": "tiktoken not installed"}
    db, _, skip = _retrieval_paths()
    if skip:
        return {"passed": None, "skip_reason": f"needs evidence DB: {skip}"}
    ensure_import_paths()
    from evidence.store import get_evidence_store  # type: ignore

    from app.context.tokenizer import TokenCounter

    store = get_evidence_store(str(db))
    try:
        rows = store.query_evidence(modality="text", limit=1000)["items"]
    finally:
        store.close()

    def is_prose(text: str) -> bool:
        markup = sum(text.count(ch) for ch in "{}\\$^_")
        return len(text) > 400 and markup / len(text) < 0.005

    samples = [r["content"] for r in rows if r.get("content") and is_prose(r["content"])]
    samples = samples[:25]
    if len(samples) < 5:
        return {"passed": None,
                "skip_reason": f"only {len(samples)} prose-like chunks in corpus"}
    heuristic, exact = TokenCounter(backend="heuristic"), TokenCounter(backend="tiktoken")
    errors = [abs(heuristic.count(s) - exact.count(s)) / exact.count(s)
              for s in samples]
    rel_error = statistics.fmean(errors)
    match = re.search(r"[0-9]*\.?[0-9]+", str(case["expected"]["relative_error"]))
    threshold = float(match.group()) if match else 0.15
    checks = {"relative_error": rel_error < threshold,
              "approximate_flag": heuristic.is_approximate}
    return {"checks": checks, "passed": all(checks.values()),
            "relative_error": round(rel_error, 4), "n_samples": len(samples),
            "threshold": threshold}


def _ctx_no_silent_truncation_driver(case: dict, ctx: LayerContext) -> Dict[str, Any]:
    """CTX-T12: every candidate is selected or dropped with reason + tokens."""
    from app.context.budgets import BudgetConfig
    from app.context.engine import ContextEngine

    n_items = int(case["inputs"]["evidence_items"])
    quota = int(case["inputs"]["evidence_quota"])
    config = BudgetConfig(
        total_tokens=8192, response_reserve=1024,
        quotas={"system": 512, "active_turn": 512, "memory": 1024,
                "evidence": quota, "artifact": 512, "tool_result": 512})
    engine = ContextEngine(config=config)
    sentence = "the transformer architecture relies on attention mechanisms entirely "
    text = sentence * 5  # ~60 tokens per item -> 20 items overflow a 500-token quota
    evidence = [_evidence_item(f"ev-{i}", text, page=i + 1, score=0.9 - i * 0.01)
                for i in range(n_items)]
    result = engine.assemble(system="sys", active_turn="q", evidence=evidence)
    selected = {it.evidence_id for it in result.evidence}
    dropped = {dr.item_id: dr for dr in result.decision.drops
               if dr.partition == "evidence"}
    accounted = selected | set(dropped)
    checks = {
        "every_candidate": accounted == {f"ev-{i}" for i in range(n_items)},
        "drops_without_reason": sum(
            1 for dr in dropped.values() if not dr.reason or dr.tokens <= 0),
    }
    checks["drops_without_reason_ok"] = checks["drops_without_reason"] == 0
    passed = checks["every_candidate"] and checks["drops_without_reason_ok"]
    return {"checks": checks, "passed": passed,
            "n_selected": len(selected), "n_dropped": len(dropped)}


_CONTEXT_DRIVERS: Dict[str, Callable[[dict, LayerContext], Dict[str, Any]]] = {
    "CTX-T01": _ctx_branch_driver,
    "CTX-T02": _ctx_branch_driver,
    "CTX-T03": _ctx_branch_driver,
    "CTX-T04": _ctx_branch_driver,
    "CTX-T05": _ctx_branch_driver,
    "CTX-T06": _ctx_anchor_driver,
    "CTX-T07": _ctx_isolation_driver,
    "CTX-T08": _ctx_compression_redline_driver,
    "CTX-T09": _ctx_compression_lineage_driver,
    "CTX-T10": _ctx_budget_model_driver,
    "CTX-T11": _ctx_tokenizer_driver,
    "CTX-T12": _ctx_no_silent_truncation_driver,
}


def run_context_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    results: Dict[str, DatasetResult] = {}
    for spec, cases in datasets.items():
        ds = DatasetResult(dataset=spec.name, status="ok")
        for case in cases:
            driver = _CONTEXT_DRIVERS.get(case["case_id"])
            if driver is None:
                ds.cases.append(CaseResult(
                    case_id=case["case_id"], dataset=spec.name, status=STATUS_SKIP,
                    error=f"no driver registered for {case['case_id']} "
                          f"(category {case.get('category')})",
                    config_fingerprint=ctx.fingerprint_hash))
                continue
            t0 = time.perf_counter()
            try:
                detail = driver(case, ctx)
            except Exception as exc:  # noqa: BLE001
                detail = {"passed": False,
                          "error": f"driver raised: {type(exc).__name__}: {exc}"}
            latency = round((time.perf_counter() - t0) * 1000, 2)
            artifact = _write_json(
                ctx.out_dir / spec.name / f"{case['case_id']}.json",
                {"case": case, "detail": detail})
            if detail.get("passed") is None:
                status = STATUS_SKIP
                error = detail.get("skip_reason", "driver could not run offline")
            else:
                status = STATUS_PASS if detail["passed"] else STATUS_FAIL
                error = None if detail["passed"] else json.dumps(
                    detail.get("checks", detail.get("error")), ensure_ascii=False)
            ds.cases.append(CaseResult(
                case_id=case["case_id"], dataset=spec.name, status=status,
                metrics={k: v for k, v in detail.items()
                         if k not in ("passed", "skip_reason")},
                error=error, artifact_uri=artifact, latency_ms=latency,
                config_fingerprint=ctx.fingerprint_hash))
        measured = [c for c in ds.cases if c.status != STATUS_SKIP]
        ds.metrics["pass_rate"] = _metric(
            "pass_rate", [float(c.status == STATUS_PASS) for c in measured])
        results[spec.name] = ds
    return results


# ---------------------------------------------------------------------------
# memory layer: memory_cases (Write/Read Gate, supersede, delete drivers)
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self) -> None:
        self._t = 1_700_000_000.0

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _run_memory_case(case: dict, work_dir: Path) -> Dict[str, Any]:
    """Drive one frozen memory case; returns failures list + detail."""
    from app.memory import (
        MemoryStore,
        ReadGate,
        ReadQuery,
        WriteGate,
        attempt_write,
        supersede,
    )
    from app.memory.metrics import cross_workspace_leakage, stale_hit_rate
    from app.memory.models import MemoryCandidate

    clock = _FakeClock()
    store = MemoryStore(str(work_dir / f"{case['case_id']}.db"), now=clock.now)
    gate, read_gate = WriteGate(now=clock.now), ReadGate(now=clock.now)
    expected = case["expected"]
    failures: List[str] = []
    detail: Dict[str, Any] = {}

    def check(cond: bool, label: str, actual: Any) -> None:
        if not cond:
            failures.append(f"{label}: got {actual!r}")

    try:
        seeded = []
        for seed in case.get("seed", []):
            decision = attempt_write(store, gate, MemoryCandidate.model_validate(seed))
            if not decision.allowed:
                failures.append(f"seed write unexpectedly rejected: {decision.reasons}")
            seeded.append(decision.record)
        clock.advance(case.get("advance_seconds", 0))
        action = case["action"]

        def recall_all(workspace: str):
            return read_gate.recall(store, ReadQuery(workspace=workspace, top_k=100))

        if action == "write":
            decision = gate.evaluate(MemoryCandidate.model_validate(case["candidate"]),
                                     store=store)
            detail["outcome"] = decision.outcome.value
            detail["reasons"] = list(decision.reasons)
            check(decision.outcome.value == expected["outcome"], "outcome",
                  detail["outcome"])
            for reason in expected.get("reasons_include", []):
                check(reason in decision.reasons, f"reason {reason!r} present",
                      detail["reasons"])
            if "recalled_values" in expected:
                recalled = recall_all(case["candidate"]["workspace"])
                values = [r.value for r in recalled]
                check(values == expected["recalled_values"], "recalled_values", values)
        elif action == "recall":
            query = ReadQuery(**case["query"])
            recalled = read_gate.recall(store, query)
            values = [r.value for r in recalled]
            detail["recalled_values"] = values
            check(values == expected["recalled_values"], "recalled_values", values)
            if "stale_hits" in expected:
                rate = stale_hit_rate(recalled, clock.now())
                detail["stale_hits"] = rate
                check(rate == expected["stale_hits"], "stale_hits", rate)
            if "cross_workspace_leakage" in expected:
                leak = cross_workspace_leakage(recalled, query.workspace)
                detail["cross_workspace_leakage"] = leak
                check(leak == expected["cross_workspace_leakage"],
                      "cross_workspace_leakage", leak)
        elif action == "supersede":
            decision = supersede(store, gate, seeded[0].memory_id,
                                 MemoryCandidate.model_validate(case["candidate"]))
            detail["outcome"] = decision.outcome.value
            check(decision.outcome.value == expected["outcome"], "outcome",
                  detail["outcome"])
            recalled = recall_all(seeded[0].workspace)
            values = [r.value for r in recalled]
            check(values == expected["recalled_values"], "recalled_values", values)
            old_status = store.get(seeded[0].memory_id).status.value
            check(old_status == expected["old_status"], "old_status", old_status)
            actions = [e["action"] for e in store.audit_trail()]
            detail["audit_actions"] = actions
            check(actions == expected["audit_actions"], "audit_actions", actions)
        elif action == "delete":
            store.delete(seeded[0].memory_id, reason="用户要求删除", actor="user")
            recalled = recall_all(seeded[0].workspace)
            values = [r.value for r in recalled]
            check(values == expected["recalled_values"], "recalled_values", values)
            actions = [e["action"] for e in store.audit_trail(seeded[0].memory_id)]
            detail["audit_actions"] = actions
            check(actions == expected["audit_actions"], "audit_actions", actions)
        else:
            failures.append(f"unknown action: {action}")
    finally:
        store.close()
    return {"failures": failures, "detail": detail}


def run_memory_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    from app.memory.metrics import write_precision

    results: Dict[str, DatasetResult] = {}
    for spec, cases in datasets.items():
        ds = DatasetResult(dataset=spec.name, status="ok")
        allowed: List[bool] = []
        legitimate: List[bool] = []
        for case in cases:
            t0 = time.perf_counter()
            try:
                outcome = _run_memory_case(case, ctx.out_dir / spec.name)
            except Exception as exc:  # noqa: BLE001
                outcome = {"failures": [f"driver raised: {type(exc).__name__}: {exc}"],
                           "detail": {}}
            latency = round((time.perf_counter() - t0) * 1000, 2)
            artifact = _write_json(
                ctx.out_dir / spec.name / f"{case['case_id']}.json",
                {"case": case, **outcome})
            failures = outcome["failures"]
            if case["action"] == "write" and not failures:
                allowed.append(outcome["detail"]["outcome"] == "allow")
                legitimate.append(case["expected"]["outcome"] == "allow")
            ds.cases.append(CaseResult(
                case_id=case["case_id"], dataset=spec.name,
                status=STATUS_FAIL if failures else STATUS_PASS,
                metrics=outcome["detail"],
                error="; ".join(failures) if failures else None,
                artifact_uri=artifact, latency_ms=latency,
                config_fingerprint=ctx.fingerprint_hash))
        passed = [c.status == STATUS_PASS for c in ds.cases]
        ds.metrics["pass_rate"] = _metric("pass_rate", [float(p) for p in passed])
        ds.metrics["write_precision"] = MetricResult(
            name="write_precision",
            value=round(write_precision(allowed, legitimate), 6) if allowed else None,
            status="measured" if allowed else "not_measured",
            kind="deterministic", n_cases=len(allowed),
            reason=None if allowed else "no_write_cases")
        results[spec.name] = ds
    return results


# ---------------------------------------------------------------------------
# verification layer (M7): answer_cases (B2 arm) + visual_cases (fixtures)
# ---------------------------------------------------------------------------

def _run_visual_cases(
    spec: DatasetSpec, cases: List[dict], ctx: LayerContext
) -> DatasetResult:
    """visual_cases: synthetic fixtures through the real M7 visual QA/eval math.

    * ``qa_fixture`` records go through ``visual_qa.check_record`` (batch
      semantics: one shared seen-figure-id set so duplicate detection
      works exactly as in production); a case passes when every expected
      defect class is detected (extras are reported, matching M7 test
      semantics).
    * ``eval_fixture`` cases go through ``visual_eval.recall_at_k`` /
      ``region_recall_at_k`` and must reproduce the frozen expectation
      exactly; realized recall values feed ViR.
    """
    from app.verification.visual_eval import recall_at_k, region_recall_at_k
    from app.verification.visual_qa import check_record

    ds = DatasetResult(dataset=spec.name, status="ok")
    seen_figure_ids: set = set()
    vir_values: List[Optional[float]] = []
    for idx, case in enumerate(cases):
        t0 = time.perf_counter()
        failures: List[str] = []
        detail: Dict[str, Any] = {}
        try:
            if case["kind"] == "qa_fixture":
                issues = check_record(
                    case["record"], index=idx,
                    page_count=case.get("page_count"),
                    seen_figure_ids=seen_figure_ids)
                got = sorted({i.issue_type.value for i in issues})
                expected = sorted(set(case["expect_issues"]))
                detail = {"got_issues": got, "expect_issues": expected,
                          "extra_issues": sorted(set(got) - set(expected))}
                missing = set(expected) - set(got)
                if missing:
                    failures.append(f"expected defects not detected: {sorted(missing)}")
            elif case["kind"] == "eval_fixture":
                checks: Dict[str, Any] = {}
                for key, want in sorted(case.items()):
                    got: Optional[float] = None
                    if key.startswith("expect_recall_at_") and "gold_figure_ids" in case:
                        k = int(key.rsplit("_", 1)[1])
                        got = recall_at_k(case["ranked_candidates"],
                                          set(case["gold_figure_ids"]), k)
                    elif (key.startswith("expect_region_recall_at_")
                          and "gold_regions" in case):
                        k = int(key.rsplit("_", 1)[1])
                        got = region_recall_at_k(
                            case["ranked_regions"], case["gold_regions"], k,
                            iou_threshold=case.get("iou_threshold", 0.5))
                    if got is None:
                        continue
                    checks[key] = {"got": got, "want": want}
                    vir_values.append(got)
                    if got != want:
                        failures.append(f"{key}: expected {want}, got {got}")
                detail["checks"] = checks
            else:
                failures.append(f"unknown visual case kind: {case['kind']!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"driver raised: {type(exc).__name__}: {exc}")
        artifact = _write_json(
            ctx.out_dir / spec.name / f"{case['case_id']}.json",
            {"case": case, "detail": detail, "failures": failures})
        ds.cases.append(CaseResult(
            case_id=case["case_id"], dataset=spec.name,
            status=STATUS_FAIL if failures else STATUS_PASS,
            metrics=detail,
            error="; ".join(failures) if failures else None,
            artifact_uri=artifact,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            config_fingerprint=ctx.fingerprint_hash))
    passed = [c.status == STATUS_PASS for c in ds.cases]
    ds.metrics["pass_rate"] = _metric("pass_rate", [float(p) for p in passed])
    # ViR family: realized visual-fact/region recall over the eval fixtures.
    ds.metrics["vir"] = _metric("vir", vir_values)
    return ds


def _run_answer_cases(
    spec: DatasetSpec, cases: List[dict], ctx: LayerContext
) -> DatasetResult:
    """answer_cases: the M7 B2 arm (planner + tools + verifier), offline.

    Per case the real M7 machinery runs: M2 ExecutionPlan -> M3
    DurableRunner (gated at answer.finalize) -> BM25 retrieval over the
    frozen M0 evidence DB -> draft -> rule-based claim verification
    (split_claims / map_claims / Verifier) -> optional one-shot evidence
    expansion -> resume of the ORIGINAL run. The only external call is
    the answer *draft* via the DeepSeek endpoint (the system under test,
    not a judge); scoring is fully deterministic rules.

    The B0 arm is deliberately NOT part of the harness: it goes through
    the running HTTP service whose retrieval is internal and not
    observable (declared asymmetry in the M7 experiment), so it is not
    reproducible offline.

    Skipped (with reason, never fabricated) when the DeepSeek key or the
    frozen evidence DB is unavailable.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return DatasetResult(
            dataset=spec.name, status="skipped",
            skip_reason="OPENAI_API_KEY not set: the B2 arm drafts answers via the "
                        "DeepSeek endpoint (answer generator, not judge); skipping "
                        "instead of fabricating answers")
    from app.verification import experiment as m7

    if not m7.BASELINE_DB.is_file():
        return DatasetResult(
            dataset=spec.name, status="skipped",
            skip_reason=f"frozen M0 evidence DB not found at {m7.BASELINE_DB}")
    try:
        evidence_items = m7.load_evidence()
    except Exception as exc:  # noqa: BLE001
        return DatasetResult(
            dataset=spec.name, status="skipped",
            skip_reason=f"could not load frozen evidence DB: {type(exc).__name__}: {exc}")
    evidence_by_id = {it["evidence_id"]: it for it in evidence_items}
    pipeline = m7.build_pipeline(evidence_items, top_k=5)

    ds = DatasetResult(dataset=spec.name, status="ok")
    work_dir = ctx.out_dir / spec.name / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    llm_log: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for case in cases:
        drafts: Dict[str, Any] = {}
        stores = m7.ArmStores(work_dir / f"b2_{case['case_id']}.db")
        registry = m7.build_arm_registry(pipeline, evidence_by_id, drafts, llm_log)
        cleared = m7.build_arm_registry(pipeline, evidence_by_id, drafts, llm_log)
        try:
            out = m7.run_b2(case, stores, registry, evidence_by_id, pipeline,
                            llm_log, cleared)
        except Exception as exc:  # noqa: BLE001
            out = {"answer": None,
                   "error": f"b2 arm raised: {type(exc).__name__}: {exc}"}
        finally:
            stores.close()
        # Deterministic gold escalation label, same derivation as the M7
        # experiment (declared in docs/CLAIM_VERIFICATION.md).
        b2v = out.get("verification", {})
        out["gold_escalate"] = bool(
            b2v.get("unsupported") or b2v.get("conflicts")
            or (out.get("fact_recall") is not None and out["fact_recall"] < 1.0)
            or (case["category"] == "refusal" and not m7.refused(out.get("answer"))))
        rows.append(out)
        artifact = _write_json(
            ctx.out_dir / spec.name / f"{case['case_id']}.json",
            {"case": case, "b2": out})
        error = out.get("error")
        ds.cases.append(CaseResult(
            case_id=case["case_id"], dataset=spec.name,
            status=STATUS_FAIL if error else STATUS_PASS,
            metrics={
                "answer_model": m7.DEEPSEEK_MODEL,
                "claims": out.get("claims"),
                "support_rate": out.get("support_rate"),
                "citation_precision": out.get("citation_precision"),
                "citation_coverage": out.get("citation_coverage"),
                "numeric_accuracy": out.get("numeric_accuracy"),
                "unsupported_rate": out.get("unsupported_rate"),
                "fact_recall": out.get("fact_recall"),
                "retrieval_rounds": out.get("retrieval_rounds"),
                "escalated": out.get("escalated"),
                "verification_action": out.get("verification_action"),
            },
            error=error, trace_uri=artifact,
            latency_ms=out.get("latency_ms"),
            config_fingerprint=ctx.fingerprint_hash))

    def col(key: str) -> List[Optional[float]]:
        return [r.get(key) for r in rows]

    ds.metrics["claim_support_rate"] = _metric("claim_support_rate", col("support_rate"))
    ds.metrics["citation_precision"] = _metric("citation_precision", col("citation_precision"))
    ds.metrics["citation_coverage"] = _metric("citation_coverage", col("citation_coverage"))
    ds.metrics["numeric_accuracy"] = _metric("numeric_accuracy", col("numeric_accuracy"))
    ds.metrics["unsupported_rate"] = _metric("unsupported_rate", col("unsupported_rate"))
    hr = ds.metrics["unsupported_rate"]
    ds.metrics["hr"] = MetricResult(  # paper HR: unsupported claims / total claims
        name="hr", value=hr.value, status=hr.status, kind=hr.kind,
        n_cases=hr.n_cases, reason=hr.reason)
    ds.metrics["fact_recall"] = _metric("fact_recall", col("fact_recall"))
    escalated = [bool(r.get("escalated")) for r in rows]
    gold = [bool(r.get("gold_escalate")) for r in rows]
    tp = sum(1 for e, g in zip(escalated, gold) if e and g)
    fp = sum(1 for e, g in zip(escalated, gold) if e and not g)
    fn = sum(1 for e, g in zip(escalated, gold) if not e and g)
    ds.metrics["escalation_precision"] = MetricResult(
        name="escalation_precision",
        value=round(tp / (tp + fp), 4) if tp + fp else None,
        status="measured" if tp + fp else "not_measured",
        kind="deterministic", n_cases=len(rows),
        reason=None if tp + fp else "no_escalations")
    ds.metrics["escalation_recall"] = MetricResult(
        name="escalation_recall",
        value=(round(tp / (tp + fn), 4) if tp + fn
               else (None if any(gold) else 1.0)),
        status="measured" if (tp + fn or not any(gold)) else "not_measured",
        kind="deterministic", n_cases=len(rows),
        reason=None if (tp + fn or not any(gold)) else "gold_escalations_missed")
    refusal_correct = sum(
        1 for case, r in zip(cases, rows)
        if case["category"] == "refusal"
        and (r.get("refused") or r.get("answer") is None))
    ds.metrics["refusal_cases_correct"] = MetricResult(
        name="refusal_cases_correct", value=float(refusal_correct),
        status="measured", kind="deterministic",
        n_cases=sum(1 for c in cases if c["category"] == "refusal"))
    return ds


def run_verification_layer(
    datasets: Dict[DatasetSpec, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    """M7 verification layer: B2 answer arm + visual fixture checks.

    Offline entry points only (see :func:`_run_answer_cases` for why B0
    is excluded). Missing prerequisites (DeepSeek key, frozen evidence
    DB, unknown case kinds) skip or fail with recorded reasons -- never
    fabricated data.
    """
    ensure_import_paths()
    handlers = {
        "verification.answer": _run_answer_cases,
        "verification.visual": _run_visual_cases,
    }
    results: Dict[str, DatasetResult] = {}
    for spec, cases in datasets.items():
        handler = handlers.get(spec.executor)
        if handler is None:
            results[spec.name] = DatasetResult(
                dataset=spec.name, status="skipped",
                skip_reason=f"no adapter registered for executor {spec.executor!r}")
            continue
        results[spec.name] = handler(spec, cases, ctx)
    return results


def _run_agent_e2e_layer(datasets, ctx):
    """S8: the ONLY executor driving the real Agent main chain end to end."""
    from app.eval.agent_e2e import run_agent_e2e_layer

    return run_agent_e2e_layer(datasets, ctx)


# ---------------------------------------------------------------------------
# Executor dispatch
# ---------------------------------------------------------------------------

LAYER_EXECUTORS: Dict[
    str, Callable[[Dict[DatasetSpec, List[dict]], LayerContext], Dict[str, DatasetResult]]
] = {
    "control": run_control_layer,
    "durable": run_durable_layer,
    "retrieval": run_retrieval_layer,
    "context": run_context_layer,
    "memory": run_memory_layer,
    "verification": run_verification_layer,
    "agent_e2e": _run_agent_e2e_layer,
}
