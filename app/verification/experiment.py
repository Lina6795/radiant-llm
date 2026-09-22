"""RADIANT-Control M7 experiment: constrained multi-agent comparison B0-B3.

Arms (frozen question set benchmarks/answer_cases.jsonl, frozen evidence
artifacts/baseline/m0-20260922/evidence.db — copied to a temp file at run
time so the M0 artifact is never opened read-write):

* B0  single AgentExecutor via real HTTP POST http://127.0.0.1:8080/query
      (service's own RAG; its retrieval is not observable, declared
      asymmetry). Falls back to a library-level DeepSeek call with BM25
      context when the service has no model initialized, and says so.
* B1  Planner+Tools: M2 ExecutionPlan executed by the M3 DurableRunner
      with real tools (BM25 retrieval via the M4 pipeline + DeepSeek draft).
* B2  B1 + Verifier (claims -> claim-evidence map -> verification decision;
      retrieve_more triggers ONE evidence-pool expansion and re-check,
      without re-generation).
* B3  B2 + Human Review Gate: human_review decisions enqueue into the M7
      review queue; a deterministic ScriptedReviewer approves/rejects; the
      ORIGINAL paused run is resumed through the M3 runner.

Metrics per case: claim support rate, citation precision/coverage against
the gold anchor page, numeric accuracy, unsupported rate, escalation
precision/recall (gold labels derived deterministically, declared in
docs/CLAIM_VERIFICATION.md), latency and token increments.

Run:  PYTHONPATH=app OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.deepseek.com \
      ./runtime/bin/python3.12 -m app.verification.experiment --out artifacts/verification/m7-20260922
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.control.models import (
    Budgets,
    ExecutionPlan,
    PlanStep,
    RetryPolicy,
    Risk,
    ToolError,
    ToolMetrics,
    ToolResult,
    ToolSpec,
    ToolStatus,
)
from app.control.registry import ToolRegistry
from app.durable.checkpoint import CheckpointStore
from app.durable.events import EventStore
from app.durable.graph import RunState
from app.durable.idempotency import PersistentIdempotencyLedger
from app.durable.lease import LeaseManager
from app.durable.runner import DurableRunner
from app.evidence.store import EvidenceStore
from app.retrieval.bm25 import BM25Index
from app.retrieval.pipeline import RetrievalConfig, RetrievalPipeline

from app.verification.claim_map import map_claims
from app.verification.claims import split_claims
from app.verification.review import ReviewQueue, ScriptedReviewer, _fact_present
from app.verification.verifier import VerificationAction, Verifier

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "benchmarks" / "answer_cases.jsonl"
BASELINE_DB = REPO_ROOT / "artifacts" / "baseline" / "m0-20260922" / "evidence.db"
SERVICE_URL = os.getenv("RADIANT_QUERY_URL", "http://127.0.0.1:8080/query")
DEEPSEEK_MODEL = os.getenv("RADIANT_TEXT_MODEL", "deepseek-v4-pro")

REFUSAL_MARKERS = (
    "not report", "not provide", "no information", "not available",
    "does not report", "not mention", "cannot determine", "not state",
    "do not report", "not specified", "no data", "not contain",
)


def strip_markdown(text: str) -> str:
    return text.replace("*", "").replace("_", " ")


# ---------------------------------------------------------------------------
# LLM + service clients
# ---------------------------------------------------------------------------

def _with_retries(fn, *, attempts: int = 2) -> Any:
    """Bounded retries: every external call fails fast (timeout <= 120s) and
    gives up after ``attempts`` tries — a case is recorded as failed, the
    experiment as a whole never hangs."""
    last: Optional[Exception] = None
    for _ in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised
            last = exc
    raise last  # type: ignore[misc]


def deepseek_chat(messages: List[Dict[str, str]], *, temperature: float = 0.0,
                  timeout: float = 120.0) -> Dict[str, Any]:
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set; DeepSeek library path unavailable")
    body = json.dumps({
        "model": DEEPSEEK_MODEL, "messages": messages,
        "temperature": temperature, "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    usage = payload.get("usage") or {}
    return {
        "content": payload["choices"][0]["message"]["content"],
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def service_query(question: str, *, timeout: float = 120.0) -> Dict[str, Any]:
    body = json.dumps({"query": question}).encode("utf-8")
    req = urllib.request.Request(
        SERVICE_URL, data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    latency = round((time.perf_counter() - t0) * 1000, 1)
    if payload.get("detail") or payload.get("error"):
        raise RuntimeError(f"service error: {payload.get('detail') or payload.get('error')}")
    return {
        "content": payload.get("response") or "",
        "latency_ms": latency,
        "prompt_tokens": None,  # not exposed by the /query endpoint
        "completion_tokens": None,
    }


# ---------------------------------------------------------------------------
# Retrieval + prompt
# ---------------------------------------------------------------------------

def build_pipeline(evidence_items: List[Dict[str, Any]], top_k: int = 5) -> RetrievalPipeline:
    cfg = RetrievalConfig(
        name="M7_bm25_topk", use_bm25=True, use_dense=False,
        fusion="rrf", final_top_k=top_k,
    )
    return RetrievalPipeline(
        cfg, bm25_index=BM25Index().build(evidence_items), evidence_items=evidence_items
    )


def retrieve(pipeline: RetrievalPipeline, question: str,
             evidence_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    trace = pipeline.run(question)
    return [evidence_by_id[c["evidence_id"]] for c in trace["final"]
            if c["evidence_id"] in evidence_by_id]


def draft_prompt(question: str, evidence: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    snippets = "\n\n".join(
        f"[p{ev.get('page')}] {(ev.get('content') or '')[:600]}" for ev in evidence
    )
    return [
        {"role": "system", "content": (
            "You answer questions strictly from the provided evidence snippets. "
            "Cite the page of every factual statement as [pN]. If the evidence "
            "does not contain the answer, say so explicitly instead of guessing."
        )},
        {"role": "user", "content": f"EVIDENCE:\n{snippets}\n\nQUESTION: {question}"},
    ]


# ---------------------------------------------------------------------------
# Durable-runner arm scaffolding (B1-B3)
# ---------------------------------------------------------------------------

class ArmStores:
    def __init__(self, db_path: Path) -> None:
        self.checkpoints = CheckpointStore(str(db_path))
        self.events = EventStore(str(db_path))
        self.leases = LeaseManager(str(db_path))
        self.ledger = PersistentIdempotencyLedger(str(db_path))

    def close(self) -> None:
        for s in (self.checkpoints, self.events, self.leases, self.ledger):
            s.close()


def build_arm_registry(
    pipeline: RetrievalPipeline,
    evidence_by_id: Dict[str, Dict[str, Any]],
    drafts: Dict[str, Dict[str, Any]],
    llm_log: List[Dict[str, Any]],
) -> ToolRegistry:
    registry = ToolRegistry()

    def _ok(output: Dict[str, Any], tokens: int = 0) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, output=output,
                          metrics=ToolMetrics(latency_ms=0, token_count=tokens))

    def search(arguments: Dict[str, Any], ctx) -> ToolResult:
        items = retrieve(pipeline, arguments["question"], evidence_by_id)
        return _ok({"evidence": [
            {"evidence_id": it["evidence_id"], "page": it.get("page"),
             "document_id": it.get("document_id"),
             "authority_level": it.get("authority_level"),
             "content": (it.get("content") or "")[:800]}
            for it in items
        ]})

    def draft(arguments: Dict[str, Any], ctx) -> ToolResult:
        items = retrieve(pipeline, arguments["question"], evidence_by_id)
        try:
            call = _with_retries(lambda: deepseek_chat(draft_prompt(arguments["question"], items)))
        except Exception as exc:
            return ToolResult(
                status=ToolStatus.TERMINAL_ERROR,
                error=ToolError(code="llm.unavailable",
                                message=f"deepseek draft failed after retries: {exc}",
                                retryable=False),
                metrics=ToolMetrics(latency_ms=0, token_count=0),
            )
        llm_log.append({"run_id": ctx.run_id, **call})
        drafts[ctx.run_id] = {"answer": call["content"], "evidence": items, "usage": call}
        return _ok({"answer": call["content"],
                    "evidence_ids": [it["evidence_id"] for it in items]},
                   tokens=(call.get("prompt_tokens") or 0) + (call.get("completion_tokens") or 0))

    def finalize(arguments: Dict[str, Any], ctx) -> ToolResult:
        answer = drafts.get(ctx.run_id, {}).get("answer", "")
        return _ok({"final_answer": answer})

    for name, handler, risk in (
        ("kb.search", search, Risk.READ_ONLY),
        ("answer.draft", draft, Risk.READ_ONLY),
        ("answer.finalize", finalize, Risk.EXTERNAL),
    ):
        registry.register(ToolSpec(
            name=name, version="0.7.0-m7", risk=risk,
            description=f"M7 experiment tool {name}",
            arguments_schema={"type": "object", "properties": {}, "required": []},
            implemented=True, handler=handler,
        ))
    return registry


def make_arm_plan(question: str) -> ExecutionPlan:
    return ExecutionPlan(
        run_id=uuid.uuid4(),
        goal=f"answer: {question[:80]}",
        steps=[
            PlanStep(step_id="search", tool="kb.search", arguments={"question": question},
                     depends_on=[], risk=Risk.READ_ONLY, timeout_ms=30_000,
                     retry_policy=RetryPolicy.NONE),
            PlanStep(step_id="draft", tool="answer.draft", arguments={"question": question},
                     depends_on=["search"], risk=Risk.READ_ONLY, timeout_ms=130_000,
                     retry_policy=RetryPolicy.NONE),
            PlanStep(step_id="finalize", tool="answer.finalize", arguments={},
                     depends_on=["draft"], risk=Risk.EXTERNAL, timeout_ms=10_000,
                     retry_policy=RetryPolicy.NONE),
        ],
        budgets=Budgets(max_tokens=100_000, max_tool_calls=10, max_wall_time_ms=600_000),
    )


def make_runner(stores: ArmStores, registry: ToolRegistry, gated: bool) -> DurableRunner:
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger,
        require_review=(lambda step: step.tool == "answer.finalize") if gated else None,
    )


# ---------------------------------------------------------------------------
# Verification + metrics
# ---------------------------------------------------------------------------

def verify_answer(answer: str, evidence: List[Dict[str, Any]], risk: str):
    claims = split_claims(answer).claims
    cmap = map_claims(claims, evidence)
    decision = Verifier().verify(
        claims=claims, claim_map=cmap, evidence_items=evidence, case_risk=risk
    )
    return claims, cmap, decision


def citation_precision(cmap, gold_anchor: Optional[Dict[str, Any]]) -> Optional[float]:
    if not gold_anchor:
        return None
    bound = [b for b in cmap.bindings if b.evidence_id]
    if not bound:
        return 0.0
    hits = sum(1 for b in bound if b.page == gold_anchor.get("page"))
    return round(hits / len(bound), 4)


def fact_recall(expected_facts: List[str], answer: Optional[str]) -> Optional[float]:
    if not expected_facts:
        return None
    if not answer:
        return 0.0
    found = sum(1 for f in expected_facts if _fact_present(f, answer))
    return round(found / len(expected_facts), 4)


def refused(answer: Optional[str]) -> bool:
    if not answer:
        return True  # no answer committed counts as not fabricating
    low = strip_markdown(answer.lower())
    return any(m in low for m in REFUSAL_MARKERS)


def measure_arm(answer: Optional[str], evidence: List[Dict[str, Any]],
                case: Dict[str, Any]) -> Dict[str, Any]:
    """Measure one arm's committed answer against the shared evidence pool."""
    if not answer:
        return {"claims": 0, "support_rate": None, "citation_precision": None,
                "citation_coverage": None, "numeric_accuracy": None,
                "unsupported_rate": None, "fact_recall": fact_recall(case["expected_facts"], None),
                "refused": True}
    claims, cmap, decision = verify_answer(answer, evidence, case["risk"])
    n = len(claims)
    return {
        "claims": n,
        "support_rate": round(len(decision.supported) / n, 4) if n else None,
        "citation_precision": citation_precision(cmap, case.get("gold_anchor")),
        "citation_coverage": cmap.coverage,
        "numeric_accuracy": decision.numeric_accuracy,
        "unsupported_rate": round(len(decision.unsupported) / n, 4) if n else None,
        "fact_recall": fact_recall(case["expected_facts"], answer),
        "refused": refused(answer),
        "verification_action": decision.action.value,
        "reason_codes": decision.reason_codes,
    }


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

def run_b0(case: Dict[str, Any], evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    t0 = time.perf_counter()
    mode = "http_service"
    call: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    try:
        call = service_query(case["question"])
    except Exception as exc:
        mode = "library_fallback"
        try:
            call = _with_retries(lambda: deepseek_chat(draft_prompt(case["question"], evidence)))
            mode = f"library_fallback (service: {type(exc).__name__})"
        except Exception as exc2:
            error = f"service: {type(exc).__name__}: {exc}; library: {type(exc2).__name__}: {exc2}"
    latency = round((time.perf_counter() - t0) * 1000, 1)
    answer = (call or {}).get("content")
    out: Dict[str, Any] = {"mode": mode, "answer": answer, "latency_ms": latency,
                           "prompt_tokens": (call or {}).get("prompt_tokens"),
                           "completion_tokens": (call or {}).get("completion_tokens")}
    if error:
        out["error"] = error
    out.update(measure_arm(answer, evidence, case))
    return out


def _failed_arm(reason: str, case: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"answer": None, "error": reason}
    out.update(measure_arm(None, [], case))
    return out


def _run_pipeline_arm(case, stores, registry, llm_log, gated):
    plan = make_arm_plan(case["question"])
    t0 = time.perf_counter()
    report = make_runner(stores, registry, gated=gated).run(plan)
    latency = round((time.perf_counter() - t0) * 1000, 1)
    draft = report.results.get("draft")
    answer = draft.output.get("answer", "") if draft else ""
    return plan, report, answer, latency


def run_b1(case, stores, registry, evidence, llm_log) -> Dict[str, Any]:
    plan, report, answer, latency = _run_pipeline_arm(case, stores, registry, llm_log, gated=False)
    if report.status is not RunState.SUCCEEDED:
        return _failed_arm(f"b1 run {report.status.value}: {report.reason}", case)
    final = report.results["finalize"].output["final_answer"]
    run_id = str(plan.run_id)
    tokens = [c for c in llm_log if c["run_id"] == run_id]
    out = {"run_id": run_id, "answer": final, "latency_ms": latency,
           "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in tokens),
           "completion_tokens": sum(c.get("completion_tokens") or 0 for c in tokens),
           "tools_executed": report.tools_executed}
    out.update(measure_arm(final, evidence, case))
    return out


def run_b2(case, stores, registry, evidence_by_id, pipeline, llm_log,
           cleared_registry) -> Dict[str, Any]:
    """B1 + verifier-as-gate-observer: decision recorded, run always resumes."""
    plan, report, answer, latency = _run_pipeline_arm(case, stores, registry, llm_log, gated=True)
    if report.status is not RunState.WAITING_REVIEW:
        return _failed_arm(f"b2 run {report.status.value}: {report.reason}", case)
    run_id = str(plan.run_id)
    evidence = [evidence_by_id[eid] for eid in
                report.results["draft"].output["evidence_ids"]]
    claims, cmap, decision = verify_answer(answer, evidence, case["risk"])

    retrieval_rounds = 1
    if decision.action is VerificationAction.RETRIEVE_MORE:
        wider = build_pipeline(list(evidence_by_id.values()), top_k=10)
        evidence = retrieve(wider, case["question"], evidence_by_id)
        claims, cmap, decision = verify_answer(answer, evidence, case["risk"])
        retrieval_rounds = 2

    t0 = time.perf_counter()
    resumed = make_runner(stores, cleared_registry, gated=False).resume(plan)
    latency += round((time.perf_counter() - t0) * 1000, 1)
    assert resumed.status is RunState.SUCCEEDED
    final = resumed.results["finalize"].output["final_answer"]
    tokens = [c for c in llm_log if c["run_id"] == run_id]

    out = {"run_id": run_id, "answer": final, "latency_ms": latency,
           "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in tokens),
           "completion_tokens": sum(c.get("completion_tokens") or 0 for c in tokens),
           "tools_executed": report.tools_executed + resumed.tools_executed,
           "retrieval_rounds": retrieval_rounds,
           "verification": json.loads(decision.model_dump_json()),
           "escalated": decision.action is VerificationAction.HUMAN_REVIEW,
           "restored_steps": resumed.resume.restored_steps}
    out.update(measure_arm(final, evidence, case))
    return out


def run_b3(case, stores, registry, evidence_by_id, pipeline, llm_log,
           cleared_registry, queue: ReviewQueue, reviewer: ScriptedReviewer) -> Dict[str, Any]:
    """B2 + human review gate: escalations enqueue, get decided, and the
    ORIGINAL run resumes (approve) or cancels (reject)."""
    plan, report, answer, latency = _run_pipeline_arm(case, stores, registry, llm_log, gated=True)
    if report.status is not RunState.WAITING_REVIEW:
        return _failed_arm(f"b3 run {report.status.value}: {report.reason}", case)
    run_id = str(plan.run_id)
    evidence = [evidence_by_id[eid] for eid in
                report.results["draft"].output["evidence_ids"]]
    claims, cmap, decision = verify_answer(answer, evidence, case["risk"])

    review_id = None
    review_decision = None
    if decision.action is VerificationAction.HUMAN_REVIEW:
        review_id = queue.enqueue(
            run_id=run_id,
            claims=[{**c.model_dump(mode="json"),
                     "status": ("unsupported" if c.claim_id in decision.unsupported else "supported")}
                    for c in claims],
            evidence_snapshot=[{"evidence_id": it["evidence_id"], "page": it.get("page"),
                                "content": (it.get("content") or "")[:300]} for it in evidence],
            risk_reasons=decision.reason_codes,
            plan=plan,
            metadata={"case_id": case["case_id"]},
        )
        decided = reviewer.apply(
            queue, review_id,
            expected_facts=case["expected_facts"], answer=answer,
        )
        review_decision = decided["decision"]
        t0 = time.perf_counter()
        resumed = queue.resume_decided(review_id, make_runner(stores, cleared_registry, gated=False))
        latency += round((time.perf_counter() - t0) * 1000, 1)
        replayed = reviewer.replay(queue.get(review_id))
        assert replayed == review_decision, "scripted reviewer decision not replayable"
    else:
        t0 = time.perf_counter()
        resumed = make_runner(stores, cleared_registry, gated=False).resume(plan)
        latency += round((time.perf_counter() - t0) * 1000, 1)

    committed = resumed.status is RunState.SUCCEEDED
    final = resumed.results["finalize"].output["final_answer"] if committed else None
    tokens = [c for c in llm_log if c["run_id"] == run_id]

    out = {"run_id": run_id, "answer": final, "latency_ms": latency,
           "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in tokens),
           "completion_tokens": sum(c.get("completion_tokens") or 0 for c in tokens),
           "tools_executed": report.tools_executed + resumed.tools_executed,
           "verification": json.loads(decision.model_dump_json()),
           "escalated": decision.action is VerificationAction.HUMAN_REVIEW,
           "review_id": review_id, "review_decision": review_decision,
           "final_run_state": resumed.status.value,
           "restored_steps": resumed.resume.restored_steps}
    out.update(measure_arm(final, evidence, case))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_cases() -> List[Dict[str, Any]]:
    return [json.loads(l) for l in CASES_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_evidence() -> List[Dict[str, Any]]:
    """Read the frozen M0 evidence.db through a temp copy — the artifact
    itself is never opened read-write."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "evidence.db"
        shutil.copy(BASELINE_DB, copy)
        store = EvidenceStore(copy)
        try:
            items: List[Dict[str, Any]] = []
            offset = 0
            while True:
                page = store.query_evidence(modality="text", limit=500, offset=offset)
                items.extend(page["items"])
                if len(items) >= page["total"]:
                    break
                offset += 500
            return items
        finally:
            store.close()


def aggregate(per_arm_cases: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    summary: Dict[str, Any] = {}
    for arm, rows in per_arm_cases.items():
        escalated = [bool(r.get("escalated")) for r in rows]
        gold = [bool(r.get("gold_escalate")) for r in rows]
        tp = sum(1 for e, g in zip(escalated, gold) if e and g)
        fp = sum(1 for e, g in zip(escalated, gold) if e and not g)
        fn = sum(1 for e, g in zip(escalated, gold) if not e and g)
        summary[arm] = {
            "cases": len(rows),
            "claim_support_rate": mean([r.get("support_rate") for r in rows]),
            "citation_precision": mean([r.get("citation_precision") for r in rows]),
            "citation_coverage": mean([r.get("citation_coverage") for r in rows]),
            "numeric_accuracy": mean([r.get("numeric_accuracy") for r in rows]),
            "unsupported_rate": mean([r.get("unsupported_rate") for r in rows]),
            "fact_recall": mean([r.get("fact_recall") for r in rows]),
            "refusal_cases_correct": sum(
                1 for r in rows
                if r.get("category") == "refusal" and (r.get("refused") or r.get("answer") is None)
            ),
            "escalations": sum(escalated),
            "escalation_precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "escalation_recall": round(tp / (tp + fn), 4) if tp + fn else (None if any(gold) else 1.0),
            "latency_ms_mean": mean([r.get("latency_ms") for r in rows]),
            "prompt_tokens_total": sum(r.get("prompt_tokens") or 0 for r in rows),
            "completion_tokens_total": sum(r.get("completion_tokens") or 0 for r in rows),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "verification" / "m7-20260922"))
    parser.add_argument("--only", nargs="*", default=None, help="case_id filter")
    args = parser.parse_args()
    out_dir = Path(args.out)
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)
    (out_dir / "work").mkdir(parents=True, exist_ok=True)

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["case_id"] in set(args.only)]
    evidence_items = load_evidence()
    evidence_by_id = {it["evidence_id"]: it for it in evidence_items}
    pipeline = build_pipeline(evidence_items, top_k=5)

    reviewer = ScriptedReviewer()
    queue = ReviewQueue(out_dir / "work" / "review_queue.db")
    llm_log: List[Dict[str, Any]] = []
    per_arm_cases: Dict[str, List[Dict[str, Any]]] = {"B0": [], "B1": [], "B2": [], "B3": []}

    for case in cases:
        cid = case["case_id"]
        print(f"[M7] {cid}: {case['question'][:60]}...")
        case_evidence = retrieve(pipeline, case["question"], evidence_by_id)

        record: Dict[str, Any] = {"case_id": cid, "question": case["question"],
                                  "category": case["category"], "risk": case["risk"],
                                  "gold_anchor": case.get("gold_anchor"),
                                  "expected_facts": case["expected_facts"]}

        record["B0"] = run_b0(case, case_evidence)

        drafts_b1: Dict[str, Any] = {}
        stores_b1 = ArmStores(out_dir / "work" / f"durable_b1_{cid}.db")
        reg_b1 = build_arm_registry(pipeline, evidence_by_id, drafts_b1, llm_log)
        try:
            record["B1"] = run_b1(case, stores_b1, reg_b1, case_evidence, llm_log)
        finally:
            stores_b1.close()

        drafts_b2: Dict[str, Any] = {}
        stores_b2 = ArmStores(out_dir / "work" / f"durable_b2_{cid}.db")
        reg_b2 = build_arm_registry(pipeline, evidence_by_id, drafts_b2, llm_log)
        reg_b2_cleared = build_arm_registry(pipeline, evidence_by_id, drafts_b2, llm_log)
        try:
            record["B2"] = run_b2(case, stores_b2, reg_b2, evidence_by_id, pipeline,
                                  llm_log, reg_b2_cleared)
        finally:
            stores_b2.close()

        drafts_b3: Dict[str, Any] = {}
        stores_b3 = ArmStores(out_dir / "work" / f"durable_b3_{cid}.db")
        reg_b3 = build_arm_registry(pipeline, evidence_by_id, drafts_b3, llm_log)
        reg_b3_cleared = build_arm_registry(pipeline, evidence_by_id, drafts_b3, llm_log)
        try:
            record["B3"] = run_b3(case, stores_b3, reg_b3, evidence_by_id, pipeline,
                                  llm_log, reg_b3_cleared, queue, reviewer)
        finally:
            stores_b3.close()

        # Gold escalation label (derived, declared in docs): the B2 verifier
        # found unsupported/conflicted claims, or expected facts are missing
        # from the B2 draft, or a refusal case committed a numeric answer.
        b2v = record["B2"].get("verification", {})
        gold_escalate = bool(
            b2v.get("unsupported") or b2v.get("conflicts")
            or (record["B2"].get("fact_recall") is not None and record["B2"]["fact_recall"] < 1.0)
            or (case["category"] == "refusal" and not refused(record["B2"].get("answer")))
        )
        for arm in ("B0", "B1", "B2", "B3"):
            record[arm]["gold_escalate"] = gold_escalate
            record[arm]["category"] = case["category"]
            per_arm_cases[arm].append(record[arm])

        (out_dir / "cases" / f"{cid}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "milestone": "M7",
        "date": "2026-09-22",
        "cases": len(cases),
        "evidence_items": len(evidence_items),
        "model": DEEPSEEK_MODEL,
        "arms": aggregate(per_arm_cases),
        "notes": [
            "B0 retrieval is internal to the running service and not observable; "
            "claim-level metrics for B0 are measured against the shared BM25 pool.",
            "Escalation gold labels are derived deterministically from B2 "
            "verification output and expected-fact presence, not human annotation.",
            "Visual cases AN-V01/AN-V02: real visual parsing deferred (no VLM key); "
            "answers here come from text evidence only.",
        ],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    queue.close()
    print(json.dumps(summary["arms"], indent=2))
    print(f"[M7] artifacts written to {out_dir}")


if __name__ == "__main__":
    main()
