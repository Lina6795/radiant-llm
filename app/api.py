import asyncio
import json
import os
import socket
import threading
from pathlib import Path
from typing import Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import radiant_llm
from utils.alerts import global_external_alerts as alerts_store
from utils.general_utilities import appendStreamEventLog, LOG_DIR

# Reuse the existing global chatbot instance and configuration
cb = radiant_llm.cb
basic_queries = radiant_llm.basic_queries


app = FastAPI(title="RADIANT-LLM API", version="0.1.0")

# Allow browser clients (React, etc.) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def log_startup_paths() -> None:
    """
    Emit one startup line so users can verify exact offline log location.
    """
    print(f"[RADIANT-LLM] Offline logs directory: {LOG_DIR}")
    appendStreamEventLog("startup", f"log_dir={LOG_DIR}")


@app.on_event("startup")
def auto_initialize_model() -> None:
    """Initialize the default chat model so /query works right after boot.

    Set RADIANT_DEFAULT_MODEL (e.g. deepseek-v4-pro) to enable; empty = skip
    (previous behaviour: model must be initialized via POST /initialize).
    Runs in a daemon thread so /health stays responsive during model load.
    """
    model = (os.getenv("RADIANT_DEFAULT_MODEL") or "").strip()
    if not model:
        return

    def _init() -> None:
        try:
            alerts = cb.initialize_models(model)
            appendStreamEventLog("auto_init", f"model={model} ok")
            print(f"[RADIANT-LLM] Auto-initialized model: {model}")
        except Exception as exc:
            appendStreamEventLog("auto_init_error", f"model={model} error={exc}")
            print(f"[RADIANT-LLM] Auto-init failed for {model}: {exc}")

    threading.Thread(target=_init, name="auto-init-model", daemon=True).start()


@app.get("/health", tags=["system"])
def healthcheck() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/basic-queries", tags=["chat"])
def get_basic_queries() -> Dict[str, Any]:
    return {"basic_queries": basic_queries}


def get_model_settings_payload() -> Dict[str, Any]:
    """Shared helper for model-settings payload (reasoning effort, temperature, supported options)."""
    initialized = cb.llm_model is not None
    supported = getattr(cb, "supported_reasoning_efforts", []) or []
    model_id = getattr(cb, "model_choice", "") or ""
    # Temperature is only adjustable for non‑GPT‑5 models; GPT‑5 families are effectively fixed at 1.0.
    temp_supported = initialized and not str(model_id).startswith("gpt-5")
    return {
        "model_initialized": initialized,
        "supported_reasoning_efforts": supported,
        "reasoning_effort": getattr(cb, "reasoning_effort", "medium"),
        "temperature": float(getattr(cb, "temperature", 1.0)),
        "temperature_supported": temp_supported,
    }


def alerts_to_text(components: List[Any]) -> List[str]:
    messages: List[str] = []
    for comp in components:
        try:
            msg = getattr(comp, "children", None)
            if isinstance(msg, (list, tuple)):
                msg = " ".join(str(x) for x in msg)
            messages.append(str(msg))
        except Exception:
            messages.append(str(comp))
    return messages


def get_skill_settings_payload() -> Dict[str, Any]:
    if hasattr(cb, "get_skill_settings_payload"):
        return cb.get_skill_settings_payload()
    return {
        "skills_directory": "",
        "auto_route_skills": True,
        "enabled_skill_ids": [],
        "enabled_modules": [],
        "enable_extended_skills": False,
        "skill_loading_level": "normal",
        "char_budget": 16_000,
        "available_modules": [],
        "bundled_skills_path": "",
        "available_bundled_skills": [],
        "available_external_skills": [],
        "warnings": [],
    }


@app.post("/initialize", tags=["chat"])
def initialize_model(payload: Dict[str, Any]) -> Dict[str, Any]:
    model = payload.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="Field 'model' is required.")

    # Call the existing method but convert its Dash alerts into plain text
    alerts_components = cb.initialize_models(model)
    messages = alerts_to_text(alerts_components)

    # Include model-settings so UI can populate reasoning effort + temperature without extra fetch
    model_settings = get_model_settings_payload()

    # Auto-create a fresh session when model is successfully initialized
    session_meta = None
    if model_settings.get("model_initialized"):
        try:
            session_meta = cb.new_session()
        except Exception as exc:
            print(f"[RADIANT-LLM] Session creation after init failed: {exc}")

    return {
        "status": "ok",
        "model": model,
        "messages": messages,
        "model_settings": model_settings,
        "session": session_meta,
    }


@app.post("/directory", tags=["chat"])
def set_directory(payload: Dict[str, Any]) -> Dict[str, Any]:
    directory = payload.get("directory")
    if not directory:
        raise HTTPException(status_code=400, detail="Field 'directory' is required.")

    # Validate here so the API can signal failure cleanly (and avoid emitting
    # any sensitive paths in error messages).
    if not os.path.isdir(directory):
        return {
            "status": "error",
            "directory": directory,
            "messages": ["Invalid directory. Please provide a valid path."],
        }

    alerts_components = cb.set_directory(directory)
    messages = alerts_to_text(alerts_components)

    return {
        "status": "ok",
        "directory": directory,
        "messages": messages,
    }


@app.get("/skill-settings", tags=["chat"])
def get_skill_settings() -> Dict[str, Any]:
    return get_skill_settings_payload()


@app.get("/skill-catalog", tags=["chat"])
def get_skill_catalog() -> Dict[str, Any]:
    settings = get_skill_settings_payload()
    external = settings.get("available_external_skills", [])
    bundled = settings.get("available_bundled_skills", [])
    return {
        "available_modules": settings.get("available_modules", []),
        "bundled_skills_path": settings.get("bundled_skills_path", ""),
        "bundled_module_count": len(bundled),
        "user_skill_count": len(external),
        "warning_count": len(settings.get("warnings", [])),
        "user_skill_packs": [
            {
                "id": s.get("id", ""),
                "slug": s.get("slug", ""),
                "title": s.get("title", ""),
                "scan_status": s.get("scan_status", "pending"),
                "scan_tier": s.get("scan_tier", ""),
                "scan_reason": s.get("scan_reason", ""),
            }
            for s in external
        ],
        "warnings": settings.get("warnings", []),
    }


@app.post("/skill-rescan", tags=["chat"])
def rescan_skills() -> Dict[str, Any]:
    from utils.radiant_skill_loader import scan_user_skills_directory
    skills_dir = getattr(cb, "skills_directory", "")
    if not skills_dir:
        return {"status": "ok", "message": "No user skills directory configured.", "warnings": []}
    _, warnings = scan_user_skills_directory(skills_dir)
    if hasattr(cb, "_trigger_tier_b_scans_bg"):
        cb._trigger_tier_b_scans_bg()
    settings = get_skill_settings_payload()
    return {"status": "ok", "warnings": warnings, "skill_settings": settings}


@app.post("/skill-scan", tags=["chat"])
def trigger_skill_scan() -> Dict[str, Any]:
    from pathlib import Path as _Path
    from utils.radiant_skill_loader import scan_user_skills_directory

    def _scan_dir(path_str: str) -> Dict[str, Any]:
        results, warnings = scan_user_skills_directory(path_str)
        blocked_names = [r["pack"] for r in results if not r.get("safe", True)]
        scanned = {r["pack"] for r in results}
        error_names = [
            w.split(":")[0].strip()
            for w in warnings
            if not w.startswith("[Skill blocked]") and w.split(":")[0].strip() not in scanned
        ]
        return {
            "safe": len(results) - len(blocked_names),
            "blocked": len(blocked_names),
            "errors": len(error_names),
            "blocked_names": blocked_names,
            "error_names": error_names,
        }

    skill_settings = get_skill_settings_payload()
    bundled_path = skill_settings.get("bundled_skills_path", "")
    if bundled_path and _Path(bundled_path).is_dir():
        try:
            dev_result: Dict[str, Any] = _scan_dir(bundled_path)
        except Exception:
            dev_result = {"safe": 0, "blocked": 0, "errors": 1, "blocked_names": [], "error_names": []}
    else:
        bundled = skill_settings.get("available_bundled_skills", [])
        dev_result = {"safe": len(bundled), "blocked": 0, "errors": 0, "blocked_names": [], "error_names": []}

    skills_dir = getattr(cb, "skills_directory", "")
    if not skills_dir:
        user_result: Dict[str, Any] = {"safe": 0, "blocked": 0, "errors": 0, "blocked_names": [], "error_names": []}
    else:
        try:
            user_result = _scan_dir(skills_dir)
        except Exception:
            user_result = {"safe": 0, "blocked": 0, "errors": 1, "blocked_names": [], "error_names": []}
        if hasattr(cb, "_trigger_tier_b_scans_bg"):
            cb._trigger_tier_b_scans_bg()

    return {
        "status": "ok",
        "dev": dev_result,
        "user": user_result,
        "skill_settings": skill_settings,
    }


@app.post("/skill-settings", tags=["chat"])
def update_skill_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    skills_directory = (payload.get("skills_directory") or "").strip()
    if skills_directory and not os.path.isdir(skills_directory):
        return {
            "status": "error",
            "messages": ["Invalid user skills path. Provide a valid parent folder (e.g. user_skills) or leave it blank."],
            "skill_settings": get_skill_settings_payload(),
        }

    enabled_modules = payload.get("enabled_modules")
    if enabled_modules is not None and not isinstance(enabled_modules, list):
        enabled_modules = None

    alerts_components = [
        cb.set_skill_settings(
            skills_directory=skills_directory,
            auto_route_skills=bool(payload.get("auto_route_skills", True)),
            enabled_skill_ids=payload.get("enabled_skill_ids") or [],
            enabled_modules=enabled_modules,
            enable_extended_skills=payload.get("enable_extended_skills"),
            skill_loading_level=payload.get("skill_loading_level"),
        )
    ]
    messages = alerts_to_text(alerts_components)
    settings = get_skill_settings_payload()
    return {
        "status": "ok",
        "messages": messages,
        "skill_settings": settings,
    }


@app.post("/query", tags=["chat"])
def query(payload: Dict[str, Any]) -> Dict[str, Any]:
    query_text = payload.get("query")
    if not query_text:
        raise HTTPException(status_code=400, detail="Field 'query' is required.")

    result = cb.convchain_api(query_text)
    if "error" in result:
        appendStreamEventLog("query_error", result["error"])
        raise HTTPException(status_code=400, detail=result["error"])
    appendStreamEventLog("query_final", f"query={query_text[:120]} result_len={len(result.get('response', '') or '')}")

    return result


@app.get("/alerts", tags=["chat"])
def get_alerts() -> Dict[str, Any]:
    # Convert any Dash Alert components (or other objects) into plain text
    texts = alerts_to_text(list(alerts_store))
    return {"alerts": texts}


@app.post("/alerts/clear", tags=["chat"])
def clear_alerts() -> Dict[str, Any]:
    alerts_store.clear()
    return {"status": "ok"}


@app.get("/model-settings", tags=["chat"])
def get_model_settings() -> Dict[str, Any]:
    """
    Return current inference settings (reasoning effort, temperature) and
    supported options for the initialized model. Used by the UI for the
    Reasoning Effort dropdown and Temperature slider.
    """
    return get_model_settings_payload()


@app.post("/model-settings", tags=["chat"])
def update_model_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update reasoning effort and/or temperature. Validates against supported
    options for the current model.
    """
    try:
        cb.apply_inference_params(
            reasoning_effort=payload.get("reasoning_effort"),
            temperature=payload.get("temperature"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "reasoning_effort": cb.reasoning_effort, "temperature": cb.temperature}


def get_context_usage_payload() -> Dict[str, Any]:
    """Shared helper for context-usage payload (memory policy + summarize eligibility)."""
    if hasattr(cb, "get_context_usage_payload"):
        return cb.get_context_usage_payload()
    usage_percent = getattr(cb, "context_usage_percent", 0.0) or 0.0
    limit_tokens = getattr(cb, "context_limit_tokens", 128_000) or 128_000
    return {
        "usage_percent": round(usage_percent, 1),
        "limit_tokens": limit_tokens,
        "warn": usage_percent >= 70,
        "compress": usage_percent >= 85,
    }


@app.get("/context-usage", tags=["chat"])
def get_context_usage() -> Dict[str, Any]:
    """
    Return current context window usage for the token-budget memory policy.
    Used by the UI for the circular context-fill indicator and summarize-memory controls.
    """
    return get_context_usage_payload()


@app.post("/memory/summarize", tags=["chat"])
def summarize_memory(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    keep_last_turns = payload.get("keep_last_turns", 2)
    if not hasattr(cb, "summarize_chat_history"):
        raise HTTPException(status_code=501, detail="Memory summarization is not available.")
    result = cb.summarize_chat_history(keep_last_turns=keep_last_turns)
    if result.get("status") == "error":
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "Memory summarization failed."),
        )
    return result


@app.get("/sessions", tags=["sessions"])
def list_sessions() -> Dict[str, Any]:
    """Return all sessions sorted newest-first."""
    return {"sessions": cb.list_sessions()}


@app.post("/sessions/new", tags=["sessions"])
def new_session() -> Dict[str, Any]:
    """Create a blank session and wire it into agent memory."""
    return cb.new_session()


@app.post("/sessions/{session_id}/load", tags=["sessions"])
def load_session(session_id: str, keep_last_n: int = 10) -> Dict[str, Any]:
    """Load a past session into agent memory and return all turns for UI display."""
    if not cb.llm_model:
        raise HTTPException(status_code=400, detail="Initialize a model before loading sessions.")
    try:
        return cb.load_session(session_id, keep_last_n=keep_last_n)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")


@app.post("/sessions/{session_id}/summarize", tags=["sessions"])
def summarize_session(session_id: str) -> Dict[str, Any]:
    """Summarise a session with the LLM and store the result in index.json."""
    if not cb.llm_model:
        raise HTTPException(status_code=400, detail="Initialize a model before summarizing.")
    try:
        return cb.summarize_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/sessions/{session_id}", tags=["sessions"])
def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete a session file and remove it from the index."""
    try:
        cb.delete_session(session_id)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/stream-query", tags=["chat"])
async def stream_query(query: str):
    """
    SSE-like streaming endpoint that sends back reasoning steps
    as they are logged, followed by the final answer.
    """
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter 'query' is required.")

    result_container: Dict[str, Any] = {}

    def run_query():
        # This will populate cb.reasoning_events as it runs
        try:
            result_container["data"] = cb.convchain_api(query)
        except Exception as e:
            result_container["error"] = str(e)

    # Start the blocking agent call in a background thread
    worker = threading.Thread(target=run_query, daemon=True)
    worker.start()

    async def event_generator():
        last_index = 0

        # Emit an initial event so UIs know the stream is live
        init_payload = {"type": "step", "log": f"[STREAM] started query: {query}"}
        appendStreamEventLog("stream_start", query)
        yield f"data: {json.dumps(init_payload)}\n\n"

        # Stream reasoning events while the worker is running
        while worker.is_alive() or last_index < len(cb.reasoning_events):
            while last_index < len(cb.reasoning_events):
                event = cb.reasoning_events[last_index]
                last_index += 1
                payload = {"type": "step", "log": event}
                appendStreamEventLog("stream_step", event)
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.25)

        # Once done, send the final answer or a structured error payload.
        data = result_container.get("data")
        if data:
            if isinstance(data, dict) and data.get("error"):
                payload = {"type": "error", "detail": str(data.get("error"))}
                appendStreamEventLog("stream_error", str(data.get("error")))
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                appendStreamEventLog(
                    "stream_final",
                    f"query={query[:120]} result_len={len(data.get('response', '') or '')}",
                )
                ctx = get_context_usage_payload()
                yield f"data: {json.dumps({'type': 'final', 'result': data, 'context_usage': ctx})}\n\n"
        elif result_container.get("error"):
            payload = {"type": "error", "detail": result_container["error"]}
            appendStreamEventLog("stream_error", result_container["error"])
            yield f"data: {json.dumps(payload)}\n\n"

        # Signal completion to the client
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Read-only Evidence Inspector API (RADIANT-Control M1)
# Served from the SQLite evidence store; deliberately independent of the
# global `cb` Chatbot singleton — the store is injected per request.
# Registered BEFORE the frontend catch-all below so it is never shadowed.
# ---------------------------------------------------------------------------
from typing import Optional as _Optional

from fastapi import Depends as _Depends, Query as _Query

from evidence.models import Modality as _Modality
from evidence.store import EvidenceStore as _EvidenceStore
from evidence.store import get_evidence_store as _get_evidence_store


@app.get("/evidence", tags=["evidence"])
def list_evidence(
    document_id: _Optional[str] = None,
    modality: _Optional[_Modality] = None,
    degraded: _Optional[bool] = None,
    workspace_id: _Optional[str] = None,
    include_degraded: bool = False,
    limit: int = _Query(100, ge=1, le=1000),
    offset: int = _Query(0, ge=0),
    store: _EvidenceStore = _Depends(_get_evidence_store),
):
    result = store.query_evidence(
        workspace_id=workspace_id,
        document_id=document_id,
        modality=modality.value if modality else None,
        degraded=degraded,
        include_degraded=include_degraded,
        limit=limit,
        offset=offset,
    )
    store.close()
    return result


@app.get("/evidence/{evidence_id}", tags=["evidence"])
def get_evidence_item(
    evidence_id: str,
    store: _EvidenceStore = _Depends(_get_evidence_store),
):
    item = store.get_evidence(evidence_id)
    store.close()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id!r} not found")
    return item


@app.get("/documents", tags=["evidence"])
def list_documents(
    document_id: _Optional[str] = None,
    workspace_id: _Optional[str] = None,
    limit: int = _Query(100, ge=1, le=1000),
    offset: int = _Query(0, ge=0),
    store: _EvidenceStore = _Depends(_get_evidence_store),
):
    result = store.list_documents(
        workspace_id=workspace_id,
        document_id=document_id,
        limit=limit,
        offset=offset,
    )
    store.close()
    return result


# ---------------------------------------------------------------------------
# RADIANT-Control M9: control-plane API (runs / reviews / benchmarks / ingest)
# plus the standalone Operations Dashboard static mount.
# Everything in this block is APPEND-ONLY and registered BEFORE the frontend
# catch-all below so API routes are never shadowed.
# ---------------------------------------------------------------------------
import sqlite3 as _sqlite3
import subprocess as _subprocess
import sys as _sys
import time as _time
import uuid as _uuid
from datetime import datetime as _datetime, timezone as _timezone

from fastapi import Header as _Header, Response as _Response

_M9_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_M9_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_M9_REPO_ROOT))

from app.control.budget import BudgetLedger as _BudgetLedger
from app.control.models import (
    Action as _Action,
    Budgets as _Budgets,
    ExecutionPlan as _ExecutionPlan,
    Risk as _Risk,
)
from app.control.planner import RulePlanner as _RulePlanner
from app.control.policy import PolicyEngine as _PolicyEngine, PolicyVerdict as _PolicyVerdict
from app.control.registry import build_default_registry as _build_default_registry
from app.control.router import RuleRouter as _RuleRouter
from app.control.schema_guard import SchemaGuard as _SchemaGuard
from app.durable.checkpoint import CheckpointStore as _CheckpointStore
from app.durable.errors import RunNotFoundError as _RunNotFoundError
from app.durable.events import EventStore as _EventStore
from app.durable.graph import RunState as _RunState
from app.durable.idempotency import PersistentIdempotencyLedger as _PersistentLedger
from app.durable.lease import LeaseManager as _LeaseManager
from app.durable.runner import DurableRunner as _DurableRunner, config_fingerprint as _config_fingerprint
from app.verification.review import ReviewQueue as _ReviewQueue, ReviewStatus as _ReviewStatus

# Run states after which the SSE event stream closes (waiting_review stays open).
_SSE_CLOSED_STATES = {_RunState.SUCCEEDED, _RunState.CANCELLED, _RunState.FAILED}
_SSE_MAX_SECONDS = 1800.0
_SSE_POLL_SECONDS = 0.5


def _m9_db_path(env_var: str, filename: str) -> str:
    """Resolve a control-plane DB path: env override > RADIANT_LLM_CONFIG_DIR > repo root."""
    override = os.getenv(env_var)
    if override:
        return override
    config_dir = os.getenv("RADIANT_LLM_CONFIG_DIR")
    base = Path(config_dir) if config_dir else _M9_REPO_ROOT
    return str(base / filename)


class RunRuntime:
    """Process-wide control-plane runtime: one DurableRunner over the durable
    SQLite stores, the M2 router/planner/guard/policy chain, the M7 review
    queue, and an in-memory plan registry (plans are needed to resume runs;
    review items persist their own plan_json, so review-driven resume also
    survives restarts)."""

    def __init__(self) -> None:
        db = _m9_db_path("RADIANT_DURABLE_DB", "durable.db")
        self.db_path = db
        self.checkpoints = _CheckpointStore(db)
        self.events = _EventStore(db)
        self.leases = _LeaseManager(db)
        self.ledger = _PersistentLedger(db)
        self.registry = _build_default_registry()
        budget_ledger = _BudgetLedger.with_defaults(["default", "readonly", "lowbudget", "full"])
        self.policy = _PolicyEngine(registry=self.registry, ledger=budget_ledger)
        self.guard = _SchemaGuard(registry=self.registry)
        self.router = _RuleRouter()
        self.planner = _RulePlanner(tool_catalog=self.registry.catalog())
        self.runner = _DurableRunner(
            registry=self.registry,
            checkpoints=self.checkpoints,
            events=self.events,
            leases=self.leases,
            ledger=self.ledger,
            owner="api-worker",
            require_review=self._step_needs_review,
        )
        self.reviews = _ReviewQueue(_m9_db_path("RADIANT_REVIEW_DB", "review_queue.db"))
        self.plans: Dict[str, _ExecutionPlan] = {}
        self.workspaces: Dict[str, str] = {}
        self.lock = threading.RLock()
        # Review-gate bookkeeping: (run_id, step_id) pairs cleared by an
        # approve/edit decision, plus the run_id of the executing thread so
        # the runner's require_review(step) hook can consult it.
        self.cleared_steps: set = set()
        self._tl = threading.local()

    def _step_needs_review(self, step) -> bool:
        run_id = getattr(self._tl, "run_id", None)
        if run_id is not None and (run_id, step.step_id) in self.cleared_steps:
            return False
        spec = self.registry.get(step.tool)
        return step.risk == _Risk.EXTERNAL or bool(spec and spec.high_risk)


_RUN_RUNTIME: _Optional[RunRuntime] = None
_RUN_RUNTIME_LOCK = threading.Lock()


def get_run_runtime() -> RunRuntime:
    global _RUN_RUNTIME
    if _RUN_RUNTIME is None:
        with _RUN_RUNTIME_LOCK:
            if _RUN_RUNTIME is None:
                _RUN_RUNTIME = RunRuntime()
    return _RUN_RUNTIME


def _review_risk_reasons(rt: RunRuntime, plan: _ExecutionPlan, step_states: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    for step in plan.steps:
        state = step_states.get(step.step_id)
        state_value = state.value if hasattr(state, "value") else str(state)
        if state_value != "waiting_review":
            continue
        spec = rt.registry.get(step.tool)
        if step.risk == _Risk.EXTERNAL:
            reasons.append(f"policy.external_review:{step.tool}")
        elif spec is not None and spec.high_risk:
            reasons.append(f"policy.high_risk_review:{step.tool}")
        else:
            reasons.append(f"policy.review:{step.tool}")
    return reasons or ["policy.review:unknown"]


def _maybe_enqueue_review(rt: RunRuntime, plan: _ExecutionPlan, workspace: str, report) -> None:
    """When a background run pauses in waiting_review, enqueue one M7 review
    item (deduplicated per run while a pending item exists)."""
    status = getattr(report, "status", None)
    if status != _RunState.WAITING_REVIEW:
        return
    run_id = str(plan.run_id)
    with rt.lock:
        for item in rt.reviews.for_run(run_id):
            if item["status"] == _ReviewStatus.PENDING.value:
                return
        rt.reviews.enqueue(
            run_id=run_id,
            claims=[],
            evidence_snapshot=[],
            risk_reasons=_review_risk_reasons(rt, plan, report.step_states),
            plan=plan,
            workspace=workspace,
            metadata={"source": "api", "goal": plan.goal, "reason": getattr(report, "reason", "")},
        )


def _execute_plan_bg(rt: RunRuntime, plan: _ExecutionPlan, workspace: str) -> None:
    rt._tl.run_id = str(plan.run_id)
    try:
        report = rt.runner.run(plan, workspace=workspace)
    except Exception as exc:  # background thread: never propagate
        appendStreamEventLog("run_bg_error", f"run_id={plan.run_id} {type(exc).__name__}: {exc}")
        return
    finally:
        rt._tl.run_id = None
    _maybe_enqueue_review(rt, plan, workspace, report)


def _resume_plan_bg(rt: RunRuntime, plan: _ExecutionPlan, workspace: str) -> None:
    rt._tl.run_id = str(plan.run_id)
    try:
        report = rt.runner.resume(plan, workspace=workspace, force_takeover=True)
    except Exception as exc:
        appendStreamEventLog("run_resume_bg_error", f"run_id={plan.run_id} {type(exc).__name__}: {exc}")
        return
    finally:
        rt._tl.run_id = None
    _maybe_enqueue_review(rt, plan, workspace, report)


def _resume_review_bg(rt: RunRuntime, review_id: str) -> None:
    item = rt.reviews.get(review_id)
    if item is None:
        return
    plan = _ExecutionPlan.model_validate_json(item["plan_json"])
    run_id = str(plan.run_id)
    # An approve/edit decision clears the review gate for the step this item
    # was enqueued for, so the resumed run passes the gate instead of
    # pausing again on the same step.
    reason = str((item.get("metadata") or {}).get("reason", ""))
    if item["status"] in {_ReviewStatus.APPROVED.value, _ReviewStatus.EDITED.value} and reason.startswith("waiting_review:"):
        with rt.lock:
            rt.cleared_steps.add((run_id, reason.split(":", 1)[1]))
    rt._tl.run_id = run_id
    try:
        report = rt.reviews.resume_decided(review_id, rt.runner)
    except Exception as exc:
        appendStreamEventLog("review_resume_bg_error", f"review_id={review_id} {type(exc).__name__}: {exc}")
        return
    finally:
        rt._tl.run_id = None
    _maybe_enqueue_review(rt, plan, item["workspace"], report)


def _plan_steps_payload(plan: _ExecutionPlan) -> List[Dict[str, Any]]:
    return [
        {
            "step_id": s.step_id,
            "tool": s.tool,
            "risk": s.risk.value,
            "depends_on": list(s.depends_on),
            "retry_policy": s.retry_policy.value,
            "timeout_ms": s.timeout_ms,
        }
        for s in plan.steps
    ]


@app.post("/runs", tags=["runs"])
def create_run(payload: Dict[str, Any], response: _Response) -> Dict[str, Any]:
    """Create a durable run: router -> planner -> guard -> policy -> DurableRunner
    (background thread). Returns the run_id; non-tool-call router outcomes
    (respond/clarify/abstain) return without a run."""
    goal = (payload.get("goal") or payload.get("query") or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Field 'goal' (or 'query') is required.")
    workspace = (payload.get("workspace") or "default").strip() or "default"

    rt = get_run_runtime()
    try:
        decision = rt.router(goal)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"router_error:{type(exc).__name__}")

    if decision.action != _Action.TOOL_CALL:
        return {
            "run_id": None,
            "status": decision.action.value,
            "reason_codes": list(decision.reason_codes),
            "detail": "router did not select tool_call; no run created",
        }

    try:
        raw_plan = rt.planner(decision, goal)
        plan = raw_plan if isinstance(raw_plan, _ExecutionPlan) else _ExecutionPlan.model_validate(raw_plan)
    except Exception:
        raise HTTPException(status_code=422, detail={"code": "planner.invalid_output", "reason_codes": []})

    budgets_override = payload.get("budgets") or {}
    if budgets_override:
        merged = plan.budgets.model_dump()
        for key in ("max_tokens", "max_tool_calls", "max_wall_time_ms"):
            if key in budgets_override:
                try:
                    merged[key] = int(budgets_override[key])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"budgets.{key} must be an integer")
        try:
            plan = plan.model_copy(update={"budgets": _Budgets(**merged)})
        except Exception:
            raise HTTPException(status_code=400, detail="invalid budgets override")

    guard = rt.guard.validate(plan)
    if not guard.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": "plan_rejected", "reason_codes": list(guard.reason_codes)},
        )

    policy = rt.policy.authorize(plan, workspace)
    if policy.verdict == _PolicyVerdict.DENY:
        raise HTTPException(
            status_code=403,
            detail={"code": "policy_denied", "reason_codes": list(policy.reason_codes), "detail": policy.detail},
        )

    run_id = str(plan.run_id)
    with rt.lock:
        rt.plans[run_id] = plan
        rt.workspaces[run_id] = workspace
    worker = threading.Thread(target=_execute_plan_bg, args=(rt, plan, workspace), daemon=True)
    worker.start()

    response.status_code = 202
    return {
        "run_id": run_id,
        "status": "running",
        "goal": goal,
        "workspace": workspace,
        "policy_verdict": policy.verdict.value,
        "reason_codes": list(policy.reason_codes),
        "config_fingerprint": _config_fingerprint(plan, rt.registry),
        "steps": _plan_steps_payload(plan),
    }


def _list_run_records(rt: RunRuntime, limit: int, offset: int) -> Dict[str, Any]:
    """Read-only run listing straight from the durable SQLite file (the M3
    CheckpointStore intentionally has no list API; this is a read-only view
    over the same file and never writes)."""
    if not os.path.exists(rt.db_path):
        return {"total": 0, "items": []}
    conn = _sqlite3.connect(f"file:{rt.db_path}?mode=ro", uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        rows = conn.execute(
            "SELECT run_id, goal, workspace, state, owner, config_fingerprint,"
            " cancel_requested, created_at, updated_at FROM runs"
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item["cancel_requested"] = bool(item["cancel_requested"])
        items.append(item)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/runs", tags=["runs"])
def list_runs(limit: int = _Query(100, ge=1, le=1000), offset: int = _Query(0, ge=0)) -> Dict[str, Any]:
    return _list_run_records(get_run_runtime(), limit, offset)


def _run_snapshot(rt: RunRuntime, run_id: str) -> Dict[str, Any]:
    record = rt.checkpoints.get_run(run_id)  # raises RunNotFoundError
    checkpoints = rt.checkpoints.load_checkpoints(run_id)
    plan = rt.plans.get(run_id)
    steps: List[Dict[str, Any]] = []
    seen = set()
    if plan is not None:
        for step in plan.steps:
            cp = checkpoints.get(step.step_id)
            steps.append(
                {
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "risk": step.risk.value,
                    "depends_on": list(step.depends_on),
                    "state": cp.state.value if cp else "pending",
                    "attempt": cp.attempt if cp else 0,
                    "error": cp.error if cp else None,
                    "artifacts": list(cp.artifacts) if cp else [],
                    "output": cp.output if cp else None,
                }
            )
            seen.add(step.step_id)
    for step_id, cp in sorted(checkpoints.items()):
        if step_id in seen:
            continue
        steps.append(
            {
                "step_id": step_id,
                "tool": None,
                "risk": None,
                "depends_on": [],
                "state": cp.state.value,
                "attempt": cp.attempt,
                "error": cp.error,
                "artifacts": list(cp.artifacts),
                "output": cp.output,
            }
        )
    return {
        "run_id": record.run_id,
        "goal": record.goal,
        "workspace": record.workspace,
        "state": record.state.value,
        "owner": record.owner,
        "config_fingerprint": record.config_fingerprint,
        "cancel_requested": record.cancel_requested,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "latest_event_seq": rt.events.latest_seq(run_id),
        "steps": steps,
    }


@app.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: str) -> Dict[str, Any]:
    rt = get_run_runtime()
    try:
        return _run_snapshot(rt, run_id)
    except _RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")


@app.get("/runs/{run_id}/events", tags=["runs"])
def stream_run_events(
    run_id: str,
    last_event_id: _Optional[str] = _Header(None, alias="Last-Event-ID"),
):
    """SSE replay/tail of the M3 event log. Reconnect with Last-Event-ID set
    to the last received ``id:`` (the event seq) to resume without gaps."""
    rt = get_run_runtime()
    try:
        rt.checkpoints.get_run(run_id)
    except _RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")

    try:
        after_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        after_seq = 0

    def event_generator():
        seq = after_seq
        started = _time.monotonic()
        last_heartbeat = started
        while True:
            events = rt.events.stream(run_id, after_seq=seq)
            for ev in events:
                seq = ev.seq
                body = dict(ev.payload)
                body["run_id"] = ev.run_id
                body["created_at"] = ev.created_at
                yield f"id: {ev.seq}\nevent: {ev.type.value}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
            state = rt.checkpoints.get_run(run_id).state
            if state in _SSE_CLOSED_STATES:
                break
            if _time.monotonic() - started > _SSE_MAX_SECONDS:
                break
            now = _time.monotonic()
            if now - last_heartbeat >= 15.0:
                last_heartbeat = now
                yield ": keep-alive\n\n"
            _time.sleep(_SSE_POLL_SECONDS)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/runs/{run_id}/cancel", tags=["runs"])
def cancel_run(run_id: str) -> Dict[str, Any]:
    rt = get_run_runtime()
    try:
        record = rt.checkpoints.get_run(run_id)
    except _RunNotFoundError:
        if run_id in rt.plans:
            # The background thread has not created the run record yet; the
            # in-memory token still reaches it (checked before every node).
            rt.runner.cancel_token(run_id).cancel()
            return {"run_id": run_id, "status": "cancel_requested"}
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if record.state in {_RunState.SUCCEEDED, _RunState.CANCELLED}:
        return {"run_id": run_id, "status": record.state.value, "detail": "run already terminal"}
    rt.runner.cancel(run_id)
    return {"run_id": run_id, "status": "cancel_requested"}


@app.post("/runs/{run_id}/resume", tags=["runs"])
def resume_run(run_id: str) -> Dict[str, Any]:
    rt = get_run_runtime()
    try:
        record = rt.checkpoints.get_run(run_id)
    except _RunNotFoundError:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if record.state in {_RunState.SUCCEEDED, _RunState.CANCELLED}:
        raise HTTPException(status_code=409, detail=f"run '{run_id}' is terminal ({record.state.value})")
    plan = rt.plans.get(run_id)
    if plan is None:
        raise HTTPException(
            status_code=409,
            detail="plan for this run is not in this process' memory; "
            "resume through its review item (POST /reviews/{id}/decision) after a restart",
        )
    workspace = rt.workspaces.get(run_id, record.workspace)
    worker = threading.Thread(target=_resume_plan_bg, args=(rt, plan, workspace), daemon=True)
    worker.start()
    return {"run_id": run_id, "status": "resuming"}


# ---------------------------------------------------------------------------
# M9: Human review queue (M7 ReviewQueue over the API runner)
# ---------------------------------------------------------------------------

@app.get("/reviews", tags=["reviews"])
def list_reviews(run_id: _Optional[str] = None) -> Dict[str, Any]:
    """List review items. Default: the pending queue. Pass run_id to see every
    item (including decided ones) for one run."""
    rt = get_run_runtime()
    items = rt.reviews.for_run(run_id) if run_id else rt.reviews.pending()
    for item in items:
        item.pop("plan_json", None)
    return {"total": len(items), "items": items}


@app.post("/reviews/{review_id}/decision", tags=["reviews"])
def decide_review(review_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Record an approve/reject/edit decision and resume the ORIGINAL run in
    the background (reject resumes with the cancel flag set)."""
    decision = (payload.get("decision") or "").strip()
    reviewer_id = (payload.get("reviewer_id") or "").strip()
    rationale = (payload.get("rationale") or "").strip()
    edited_answer = payload.get("edited_answer")
    if decision not in {"approve", "reject", "edit"}:
        raise HTTPException(status_code=400, detail="decision must be one of approve|reject|edit")
    if not reviewer_id:
        raise HTTPException(status_code=400, detail="Field 'reviewer_id' is required.")
    rt = get_run_runtime()
    try:
        item = rt.reviews.decide(
            review_id,
            decision=decision,
            reviewer_id=reviewer_id,
            rationale=rationale,
            edited_answer=edited_answer,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"review '{review_id}' not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    item.pop("plan_json", None)
    worker = threading.Thread(target=_resume_review_bg, args=(rt, review_id), daemon=True)
    worker.start()
    return {"status": "decided", "resume": "started", "item": item}


# ---------------------------------------------------------------------------
# M9: Benchmark runs (M8 eval runner as a background subprocess)
# ---------------------------------------------------------------------------

_EVAL_OUT_ROOT = _M9_REPO_ROOT / "artifacts" / "eval"
_DEFAULT_BENCHMARK_BASELINE = _EVAL_OUT_ROOT / "m8-baseline-20260922"
_BENCHMARK_JOBS: Dict[str, Dict[str, Any]] = {}
_BENCHMARK_LOCK = threading.Lock()


def _benchmark_layers() -> List[str]:
    from eval.registry import LAYERS

    return list(LAYERS)


def _write_benchmark_job(out_dir: Path, job: Dict[str, Any]) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _watch_benchmark(job_id: str, proc: "_subprocess.Popen", out_dir: Path) -> None:
    exit_code = proc.wait()
    with _BENCHMARK_LOCK:
        job = _BENCHMARK_JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "done" if exit_code == 0 else "failed"
        job["exit_code"] = exit_code
        job["finished_at"] = _datetime.now(_timezone.utc).isoformat()
        _write_benchmark_job(out_dir, job)


@app.post("/benchmarks/run", tags=["benchmarks"])
def run_benchmark(payload: Dict[str, Any], response: _Response) -> Dict[str, Any]:
    """Run the M8 eval harness in a background subprocess. ``layers`` is
    optional (default: all registered layers)."""
    known = _benchmark_layers()
    layers = payload.get("layers")
    if layers is None:
        layers = known
    if not isinstance(layers, list) or not layers:
        raise HTTPException(status_code=400, detail="'layers' must be a non-empty list (or omitted for all).")
    unknown = [l for l in layers if l not in known]
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_layers", "unknown": unknown, "known": known})

    baseline = payload.get("baseline")
    baseline_path = Path(baseline) if baseline else _DEFAULT_BENCHMARK_BASELINE
    if not baseline_path.is_absolute():
        baseline_path = _M9_REPO_ROOT / baseline_path
    if not baseline_path.exists():
        raise HTTPException(status_code=400, detail=f"baseline not found: {baseline_path}")

    benchmark_id = "m9-" + _datetime.now(_timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + _uuid.uuid4().hex[:6]
    out_dir = _EVAL_OUT_ROOT / benchmark_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "runner.log"
    cmd = [_sys.executable, "-m", "eval.runner"]
    if layers == known:
        cmd.append("--all")
    else:
        for layer in layers:
            cmd += ["--layer", layer]
    cmd += ["--out", str(out_dir), "--baseline", str(baseline_path)]
    env = dict(os.environ)
    env["PYTHONPATH"] = "app" + os.pathsep + env.get("PYTHONPATH", "")

    job = {
        "benchmark_id": benchmark_id,
        "status": "running",
        "layers": layers,
        "baseline": str(baseline_path),
        "out_dir": str(out_dir),
        "log": str(log_path),
        "started_at": _datetime.now(_timezone.utc).isoformat(),
        "finished_at": None,
        "exit_code": None,
    }
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        proc = _subprocess.Popen(cmd, cwd=str(_M9_REPO_ROOT), stdout=log_fh, stderr=_subprocess.STDOUT, env=env)
    except Exception as exc:
        log_fh.close()
        raise HTTPException(status_code=500, detail=f"failed to start eval runner: {exc}")
    job["pid"] = proc.pid
    with _BENCHMARK_LOCK:
        _BENCHMARK_JOBS[benchmark_id] = job
    _write_benchmark_job(out_dir, job)
    watcher = threading.Thread(target=_watch_benchmark, args=(benchmark_id, proc, out_dir), daemon=True)
    watcher.start()

    response.status_code = 202
    return {"benchmark_id": benchmark_id, "status": "running", "layers": layers, "out_dir": str(out_dir)}


def _benchmark_report_digest(out_dir: Path) -> _Optional[Dict[str, Any]]:
    report_path = out_dir / "report.json"
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    failed_cases = [
        {
            "layer": layer,
            "case_id": c.get("case_id"),
            "dataset": c.get("dataset"),
            "status": c.get("status"),
            "error": c.get("error"),
        }
        for layer, lr in (report.get("layers") or {}).items()
        for c in (lr.get("cases") or [])
        if c.get("status") == "fail"
    ]
    return {
        "run_id": report.get("run_id"),
        "created_at": report.get("created_at"),
        "summary": report.get("summary"),
        "metrics_flat": report.get("metrics_flat"),
        "baseline_comparison": report.get("baseline_comparison"),
        "failed_cases": failed_cases,
    }


def _read_benchmark_job(out_dir: Path) -> _Optional[Dict[str, Any]]:
    job_path = out_dir / "job.json"
    if not job_path.exists():
        return None
    try:
        return json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/benchmarks/{benchmark_id}", tags=["benchmarks"])
def get_benchmark(benchmark_id: str) -> Dict[str, Any]:
    with _BENCHMARK_LOCK:
        job = _BENCHMARK_JOBS.get(benchmark_id)
        job = dict(job) if job else None
    out_dir = _EVAL_OUT_ROOT / benchmark_id
    if job is None:
        job = _read_benchmark_job(out_dir)
    if job is None and not out_dir.exists():
        raise HTTPException(status_code=404, detail=f"benchmark '{benchmark_id}' not found")
    digest = _benchmark_report_digest(out_dir)
    if job is None:
        job = {"benchmark_id": benchmark_id, "status": "done" if digest else "unknown", "out_dir": str(out_dir)}
    job["report"] = digest
    return job


@app.get("/benchmarks", tags=["benchmarks"])
def list_benchmarks() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    seen = set()
    with _BENCHMARK_LOCK:
        for job in _BENCHMARK_JOBS.values():
            seen.add(job["benchmark_id"])
            items.append({k: v for k, v in job.items() if k != "report"})
    if _EVAL_OUT_ROOT.exists():
        for child in sorted(_EVAL_OUT_ROOT.iterdir(), reverse=True):
            if not child.is_dir() or child.name in seen:
                continue
            digest = _benchmark_report_digest(child)
            if digest is None:
                continue
            items.append(
                {
                    "benchmark_id": child.name,
                    "status": "done",
                    "created_at": digest.get("created_at"),
                    "summary": digest.get("summary"),
                }
            )
    return {"total": len(items), "items": items}


# ---------------------------------------------------------------------------
# M9: document ingestion jobs (parse -> evidence store)
# ---------------------------------------------------------------------------

_INGEST_JOBS: Dict[str, Dict[str, Any]] = {}
_INGEST_LOCK = threading.Lock()
_KB_JSONL_FILES = ("01_chunks_kb.jsonl", "02_visuals_kb.jsonl", "03_metadata_kb.jsonl")


def _ingest_job_update(job_id: str, **fields: Any) -> None:
    with _INGEST_LOCK:
        job = _INGEST_JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _run_ingest_job(job_id: str, input_dir: Path, workspace_id: str, source_dir: _Optional[str], vision_model: _Optional[str], parse: bool, text_mode: str) -> None:
    _ingest_job_update(job_id, status="running", started_at=_datetime.now(_timezone.utc).isoformat())
    try:
        kb_dir = input_dir
        has_kb = any((input_dir / name).exists() for name in _KB_JSONL_FILES)
        if not has_kb:
            has_pdfs = any(p.suffix.lower() == ".pdf" for p in input_dir.rglob("*") if p.is_file())
            if has_pdfs and parse:
                _ingest_job_update(job_id, phase="parse")
                from utils.vp_config import ParserConfig
                from utils.vp_pipeline import run_pipeline

                config_kwargs: Dict[str, Any] = {
                    "input_dir": str(input_dir),
                    "output_dir": str(input_dir),
                    "text_mode": text_mode,
                }
                if vision_model:
                    config_kwargs["gpt_vision_model"] = vision_model
                parse_result = run_pipeline(ParserConfig(**config_kwargs))
                _ingest_job_update(job_id, parse_result={k: v for k, v in parse_result.items() if isinstance(v, (int, str))})
                has_kb = any((input_dir / name).exists() for name in _KB_JSONL_FILES)
            if not has_kb:
                _ingest_job_update(
                    job_id,
                    status="failed",
                    finished_at=_datetime.now(_timezone.utc).isoformat(),
                    error="no KB JSONL files found in input_dir and nothing to parse "
                    "(expected 01_chunks_kb.jsonl etc. or PDF files with parse=true)",
                )
                return
        _ingest_job_update(job_id, phase="ingest")
        store = _get_evidence_store()
        try:
            result = store.ingest_directory(
                kb_dir,
                workspace_id=workspace_id,
                source_dir=source_dir,
                vision_model=vision_model,
            )
        finally:
            store.close()
        _ingest_job_update(
            job_id,
            status="done",
            phase=None,
            finished_at=_datetime.now(_timezone.utc).isoformat(),
            result={
                "workspace_id": result.get("workspace_id"),
                "documents": result.get("documents"),
                "new_records": result.get("new_records"),
                "failed": result.get("failed"),
                "rejected_rows": len(result.get("rejected_rows") or []),
            },
        )
    except Exception as exc:  # background job: record, never raise
        _ingest_job_update(
            job_id,
            status="failed",
            phase=None,
            finished_at=_datetime.now(_timezone.utc).isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )


@app.post("/documents/ingest", tags=["evidence"])
def ingest_documents(payload: Dict[str, Any], response: _Response) -> Dict[str, Any]:
    """Ingest a directory into the evidence store in the background. The
    directory either already contains KB JSONL output (01_chunks_kb.jsonl …)
    or PDFs to parse first (parse=true, default)."""
    input_dir_raw = (payload.get("input_dir") or "").strip()
    if not input_dir_raw:
        raise HTTPException(status_code=400, detail="Field 'input_dir' is required.")
    input_dir = Path(input_dir_raw)
    if not input_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"input_dir is not a directory: {input_dir_raw}")
    workspace_id = (payload.get("workspace_id") or "default").strip() or "default"
    parse = bool(payload.get("parse", True))
    text_mode = payload.get("text_mode") or "lightweight"
    if text_mode not in ("nougat", "lightweight"):
        raise HTTPException(status_code=400, detail="text_mode must be 'nougat' or 'lightweight'")
    job_id = "ing-" + _uuid.uuid4().hex[:12]
    with _INGEST_LOCK:
        _INGEST_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "phase": None,
            "input_dir": str(input_dir),
            "workspace_id": workspace_id,
            "created_at": _datetime.now(_timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
    worker = threading.Thread(
        target=_run_ingest_job,
        args=(job_id, input_dir, workspace_id, payload.get("source_dir"), payload.get("vision_model"), parse, text_mode),
        daemon=True,
    )
    worker.start()
    response.status_code = 202
    return {"job_id": job_id, "status": "queued"}


@app.get("/documents/ingest/{job_id}", tags=["evidence"])
def get_ingest_job(job_id: str) -> Dict[str, Any]:
    with _INGEST_LOCK:
        job = _INGEST_JOBS.get(job_id)
        job = dict(job) if job else None
    if job is None:
        raise HTTPException(status_code=404, detail=f"ingest job '{job_id}' not found")
    return job


# ---------------------------------------------------------------------------
# M9: standalone Operations Dashboard (vanilla JS, no build step)
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard-static"
if _DASHBOARD_DIR.is_dir():
    @app.get("/dashboard", include_in_schema=False)
    def dashboard_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/dashboard/")

    app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")


# Serve built React frontend if present (used in Docker image)
# IMPORTANT: Mount static files LAST so API routes take precedence
def resolveFrontendDist() -> Path | None:
    candidates = [
        # 1) Next to this module (e.g., editable dev install)
        Path(__file__).resolve().parent / "frontend-dist",
        # 2) Current working directory (e.g., running api.py directly)
        Path.cwd() / "frontend-dist",
        # 3) Docker image location (we COPY dist -> /radiant-llm/frontend-dist)
        Path("/radiant-llm/frontend-dist"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None

FRONTEND_DIST = resolveFrontendDist()
if FRONTEND_DIST is not None:
    # Mount static assets (JS, CSS, etc.) at /assets
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir)),
            name="static-assets",
        )
    
    # Catch-all route: serve index.html for any route not matched by API endpoints above
    # FastAPI matches routes in registration order, so API routes take precedence
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        index_file = FRONTEND_DIST / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        raise HTTPException(status_code=404, detail="Frontend not found")


def chooseAvailablePort(preferred_port: int) -> int:
    """
    Return preferred_port if free; otherwise ask OS for any free port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", preferred_port))
            return preferred_port
        except OSError:
            # Keep fallback logic consistent with existing shared utility.
            return int(radiant_llm.free_port_finder())


def run():
    """
    Convenience entry point for `python -m radiant_llm.api` or
    for use as a console_script when we package this project.
    """
    import uvicorn

    host = os.getenv("RADIANT_LLM_HOST", "0.0.0.0")
    preferred_port = int(os.getenv("RADIANT_LLM_PORT", os.getenv("PORT", "8080")))
    in_container = os.getenv(
        "RADIANT_LLM_IN_DOCKER",
        os.getenv("IN_DOCKER", "false"),
    ).lower() in {"1", "true", "yes"}

    if in_container:
        # Deterministic container behavior: fixed internal port only.
        selected_port = preferred_port
    else:
        selected_port = chooseAvailablePort(preferred_port)
        if selected_port != preferred_port:
            print(
                f"[RADIANT-LLM] Preferred port {preferred_port} unavailable; "
                f"using free port {selected_port}."
            )

    uvicorn.run(
        "api:app",
        host=host,
        port=selected_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
