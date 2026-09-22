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
