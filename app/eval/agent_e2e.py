"""S8: agent_e2e layer -- Eval Adapter driving the REAL Agent main chain
(Router -> Planner -> Guard -> Policy -> DurableRunner -> real adapters ->
answer.verify) instead of offline module calls.

Every case produces: run completion, verify_action, claim verdicts, anchor
hit, fact recall, latency. Answers finalized (accept) optionally go through a
real LLM judge (small sample) with prompt/model/version recorded.

Verdict semantics (S10): a case is ``pass`` ONLY when the run succeeded and
the verifier accepted the answer. ``review`` means the chain executed but the
answer was finalized with unsupported claims and escalated to human review --
recorded separately, never counted as pass. Every case carries its run trace,
config fingerprint and latency so aggregates trace back to case + config.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters import (
    CaseResult,
    DatasetResult,
    LayerContext,
    _metric,
    ensure_import_paths,
)

JUDGE_MODEL_ENV = "RADIANT_DEFAULT_MODEL"


def _build_runtime(workspace: str = "default"):
    ensure_import_paths()
    from app.control.budget import BudgetLedger
    from app.control.models import ExecutionPlan
    from app.control.planner import RulePlanner
    from app.control.policy import PolicyEngine
    from app.control.registry import build_default_registry
    from app.control.router import RuleRouter
    from app.control.schema_guard import SchemaGuard
    from app.durable.checkpoint import CheckpointStore
    from app.durable.events import EventStore
    from app.durable.idempotency import PersistentIdempotencyLedger
    from app.durable.lease import LeaseManager
    from app.durable.runner import DurableRunner
    import tempfile
    from pathlib import Path

    db = str(Path(tempfile.mkdtemp(prefix="s8-eval-")) / "durable.db")
    # Warm the lazy legacy import OUTSIDE the first node's 5s timeout (D6:
    # cold import takes 30-80s and would otherwise fail s1-search).
    kb_dir = (os.getenv("RADIANT_EVIDENCE_KB_DIR") or "").strip()
    if kb_dir:
        from utils.pdf_helpers import direct_jsonl_kb_search

        direct_jsonl_kb_search(working_directory=kb_dir, query="warmup", max_hits=1)
    registry = build_default_registry(evidence="real")
    events = EventStore(db)
    runner = DurableRunner(
        registry=registry,
        checkpoints=CheckpointStore(db),
        events=events,
        leases=LeaseManager(db),
        ledger=PersistentIdempotencyLedger(db),
    )
    return {
        "router": RuleRouter(),
        "planner": RulePlanner(tool_catalog=registry.catalog()),
        "guard": SchemaGuard(registry=registry),
        "policy": PolicyEngine(
            registry=registry,
            ledger=BudgetLedger.with_defaults(["default", "readonly", "lowbudget", "full"]),
        ),
        "runner": runner,
        "events": events,
        "ExecutionPlan": ExecutionPlan,
    }


def _dump_trace(out_dir: Path, case_id: str, run_id: str, events) -> str:
    """Persist the run's event stream so every case verdict traces back to
    an auditable execution trace."""
    trace_dir = out_dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"{case_id}.json"
    payload = {
        "run_id": run_id,
        "events": [
            {"seq": e.seq, "type": e.type.value, "payload": e.payload,
             "created_at": e.created_at}
            for e in events.stream(run_id)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    return str(path)


def _judge_answer(question: str, answer: str, claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Real LLM judge over ONE finalized answer. Records prompt/model/version;
    verdict + confidence come back for the agreement field."""
    from app.verification import answer_tools

    prompt = (
        "You are a strict fact-check judge. Given a question, an answer, and the "
        "claim-level verification results, decide whether the answer faithfully "
        "answers the question using ONLY the cited evidence. Return JSON "
        '{"verdict": "pass"|"fail", "confidence": 0..1, "rationale": str}.'
    )
    payload = json.dumps({"question": question, "answer": answer, "claims": claims},
                         ensure_ascii=False)
    chat = answer_tools._chat or answer_tools._default_chat()
    call = chat([{"role": "system", "content": prompt}, {"role": "user", "content": payload}])
    verdict = None
    try:
        parsed = json.loads(call["content"].strip().strip("`").removeprefix("json").strip())
        verdict = parsed.get("verdict")
        confidence = parsed.get("confidence")
    except Exception:
        confidence = None
    return {
        "judge_prompt": prompt,
        "judge_model": (os.getenv(JUDGE_MODEL_ENV) or "deepseek-chat"),
        "judge_version": "judge-v1",
        "judge_verdict": verdict,
        "judge_confidence": confidence,
        "judge_latency_ms": call.get("latency_ms"),
    }


def run_agent_e2e_layer(
    datasets: Dict[Any, List[dict]], ctx: LayerContext
) -> Dict[str, DatasetResult]:
    ensure_import_paths()
    results: Dict[str, DatasetResult] = {}
    runtime = _build_runtime()
    judge_budget = int(os.getenv("RADIANT_E2E_JUDGE_MAX", "1"))
    judged = 0

    for spec, cases in datasets.items():
        case_results: List[CaseResult] = []
        for case in cases:
            question = case.get("question") or (case.get("input") or {}).get("question")
            if not question:
                case_results.append(CaseResult(
                    case_id=case.get("case_id", "?"), dataset=spec.name, status="skip",
                    error="no question field"))
                continue
            goal = question
            if "Evidence ID" not in goal:
                goal = goal.rstrip("? ") + "? Answer with the PDF name, page number and Evidence ID."
            started = time.monotonic()
            try:
                decision = runtime["router"](goal)
                if decision.action.value != "tool_call":
                    case_results.append(CaseResult(
                        case_id=case["case_id"], dataset=spec.name, status="fail",
                        error=f"router action {decision.action.value}"))
                    continue
                plan = runtime["ExecutionPlan"].model_validate(runtime["planner"](decision, goal))
                guard = runtime["guard"].validate(plan)
                if not guard.ok:
                    case_results.append(CaseResult(
                        case_id=case["case_id"], dataset=spec.name, status="fail",
                        error=f"guard rejected: {guard.reason_codes}"))
                    continue
                policy = runtime["policy"].authorize(plan, "default")
                if policy.verdict.value != "allow":
                    case_results.append(CaseResult(
                        case_id=case["case_id"], dataset=spec.name, status="fail",
                        error=f"policy {policy.verdict.value}"))
                    continue
                report = runtime["runner"].run(plan, workspace="default")
            except Exception as exc:  # noqa: BLE001 - recorded, never fabricated
                case_results.append(CaseResult(
                    case_id=case["case_id"], dataset=spec.name, status="fail",
                    error=f"{type(exc).__name__}: {exc}"))
                continue
            latency_ms = round((time.monotonic() - started) * 1000, 1)

            verify = (report.results or {}).get("s5-verify")
            output = (verify.output or {}) if verify else {}
            failing = {sid: (r.error.code if r.error else "")
                       for sid, r in (report.results or {}).items()
                       if r.status.value != "success"}
            claims = output.get("claims") or []
            answer = output.get("answer") or ""
            gold = case.get("gold_anchor") or {}
            facts = case.get("expected_facts") or []
            cited_pages = sorted({c.get("page") for c in claims if c.get("page") is not None})
            gold_page = gold.get("page")
            action = output.get("verify_action")
            claims_supported = sum(1 for c in claims if c["verdict"] == "supported")
            metrics = {
                "verify_action": action,
                "claims_total": len(claims),
                "claims_supported": claims_supported,
                "claims_unsupported": sum(1 for c in claims if c["verdict"] != "supported"),
                "claims_cited": sum(1 for c in claims if c.get("evidence_ids")),
                "supported_claim_rate": (round(claims_supported / len(claims), 6)
                                         if claims else None),
                "anchor_hit": bool(gold_page is not None and gold_page in cited_pages),
                "fact_recall": ((sum(1 for f in facts if f.lower() in answer.lower()) / len(facts))
                                if (facts and answer) else None),
                "latency_ms": latency_ms,
                "run_status": report.status.value,
                "failing_steps": failing or None,
                "revise_used": output.get("revise_used"),
            }
            if action == "accept" and judged < judge_budget:
                judged += 1
                try:
                    metrics["judge"] = _judge_answer(question, answer, claims)
                except Exception as exc:  # noqa: BLE001
                    metrics["judge"] = {"judge_error": f"{type(exc).__name__}: {exc}",
                                        "judge_model": (os.getenv(JUDGE_MODEL_ENV) or "deepseek-chat"),
                                        "judge_version": "judge-v1"}
            # Honest verdict: pass ONLY on verifier accept. "review" means the
            # chain executed but the answer escalated to human review with
            # unsupported claims -- counted separately, never as pass.
            if report.status.value == "succeeded" and action == "accept":
                status = "pass"
            elif report.status.value == "succeeded" and action == "review":
                status = "review"
            else:
                status = "fail"
            trace_uri = _dump_trace(ctx.out_dir, case["case_id"],
                                    str(plan.run_id), runtime["events"])
            answer_dir = ctx.out_dir / "answers"
            answer_dir.mkdir(parents=True, exist_ok=True)
            artifact_uri = str(answer_dir / f"{case['case_id']}.json")
            Path(artifact_uri).write_text(json.dumps(
                {"case_id": case["case_id"], "question": question, "answer": answer,
                 "claims": claims, "verify_action": action},
                ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            case_results.append(CaseResult(
                case_id=case["case_id"], dataset=spec.name, status=status,
                metrics=metrics, artifact_uri=artifact_uri, trace_uri=trace_uri,
                latency_ms=latency_ms,
                config_fingerprint=ctx.fingerprint_hash))
        n = len(case_results)
        n_executed = sum(1 for c in case_results
                         if c.metrics.get("run_status") == "succeeded")
        layer_metrics = {
            "execution_rate": _metric("execution_rate",
                                      [1.0 if c.metrics.get("run_status") == "succeeded" else 0.0
                                       for c in case_results]),
            "accept_rate": _metric("accept_rate",
                                   [1.0 if c.status == "pass" else 0.0
                                    for c in case_results]),
            "review_rate": _metric("review_rate",
                                   [1.0 if c.status == "review" else 0.0
                                    for c in case_results]),
            "supported_claim_rate": _metric(
                "supported_claim_rate",
                [c.metrics.get("supported_claim_rate") for c in case_results]),
            "anchor_hit_rate": _metric(
                "anchor_hit_rate",
                [float(c.metrics["anchor_hit"]) if "anchor_hit" in c.metrics else None
                 for c in case_results]),
            "fact_recall": _metric("fact_recall",
                                   [c.metrics.get("fact_recall") for c in case_results]),
        }
        failed = [c for c in case_results if c.status == "fail"]
        results[spec.name] = DatasetResult(
            dataset=spec.name,
            status="failed" if failed else "ok",
            cases=case_results,
            metrics=layer_metrics,
        )
        print(f"[agent_e2e] {spec.name}: {n} cases, executed {n_executed}, "
              f"accept {sum(1 for c in case_results if c.status == 'pass')}, "
              f"review {sum(1 for c in case_results if c.status == 'review')}, "
              f"fail {len(failed)}", flush=True)
    return results
