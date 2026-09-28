"""S1-3: real evidence.search handler — read-only, no LLM, no writes.

Two-stage lookup:
1. Reuse the original keyword search over the KB JSONL files
   (utils.pdf_helpers.direct_jsonl_kb_search) to get scored candidates.
   The pipeline is wrapped, never rewritten.
2. Resolve each candidate's (workspace_id, document_id, chunk_id) to a real
   evidence_id via the Evidence Store. Candidates that cannot be resolved
   are counted in ``unresolved_count`` — never silently promoted.

KB directory comes from RADIANT_EVIDENCE_KB_DIR (runtime config, nothing
hardcoded); the store is resolved by the standard factory
(get_evidence_store: RADIANT_EVIDENCE_DB / config dir / cwd).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.control.models import ToolError, ToolMetrics, ToolResult, ToolStatus
from app.evidence.adapter import make_document_id
from app.evidence.store import get_evidence_store

KB_DIR_ENV = "RADIANT_EVIDENCE_KB_DIR"
MODE_ENV = "RADIANT_EVIDENCE_SEARCH_MODE"  # auto (default) | hybrid | keyword
TOOL_VERSION = "evidence.search/2"


def _terminal(code: str, message: str, started: float, provenance) -> ToolResult:
    return ToolResult(
        status=ToolStatus.TERMINAL_ERROR,
        error=ToolError(code=code, message=message, retryable=False),
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000), token_count=0),
        provenance=provenance,
    )


def evidence_search_handler(arguments: Dict[str, Any], ctx) -> ToolResult:
    started = time.monotonic()
    kb_dir = (os.getenv(KB_DIR_ENV) or "").strip()
    if not kb_dir or not Path(kb_dir).is_dir():
        return _terminal(
            "evidence.kb_dir_missing",
            f"{KB_DIR_ENV} is not set or not a directory; real evidence.search is unavailable",
            started,
            ctx.provenance,
        )

    query = arguments["query"]
    top_k = max(1, min(int(arguments.get("top_k") or 3), 20))
    workspace = arguments.get("workspace_id") or ctx.workspace
    fallback_reason = None
    mode = (os.getenv(MODE_ENV) or "auto").strip().lower()
    if mode not in ("auto", "hybrid", "keyword"):
        return _terminal(
            "evidence.search_mode_invalid",
            f"{MODE_ENV} must be auto|hybrid|keyword, got {mode!r}",
            started,
            ctx.provenance,
        )

    if mode != "keyword":
        store = get_evidence_store()
        try:
            try:
                from app.evidence.hybrid_search import HybridSearchUnavailable, hybrid_search

                result = hybrid_search(query, top_k, workspace, store)
            except HybridSearchUnavailable as exc:
                if mode == "hybrid":
                    return _terminal(exc.code, exc.message, started, ctx.provenance)
                fallback_reason = exc.code
                result = None
            if result is not None:
                latency = int((time.monotonic() - started) * 1000)
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    output={
                        "query": query,
                        "hits": result["hits"],
                        "mock": False,
                        "retrieval": "hybrid_bm25_dense_rrf",
                        "unresolved_count": result["unresolved_count"],
                        "lanes": result["lanes"],
                        "rrf_k": result["rrf_k"],
                        "recall_k": result["recall_k"],
                        "index_fingerprint": result["index_fingerprint"],
                        "gate": result["gate"],
                        "sufficiency": result["sufficiency"],
                        "sufficiency_reasons": result["sufficiency_reasons"],
                        "retrieve_more": result["retrieve_more"],
                    },
                    metrics=ToolMetrics(latency_ms=latency, token_count=0),
                    provenance=ctx.provenance,
                )
        finally:
            try:
                store.close()
            except Exception:
                pass

    # Keyword lane (S1-3): original JSONL keyword search resolved to evidence.
    # Lazy import: utils.pdf_helpers pulls the legacy stack (chroma, matplotlib,
    # general_utilities) which expects service env vars; keep module import light.
    from utils.pdf_helpers import direct_jsonl_kb_search

    search = direct_jsonl_kb_search(working_directory=kb_dir, query=query, max_hits=max(top_k * 4, 8))

    hits = []
    unresolved = 0
    store = get_evidence_store()
    try:
        doc_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for hit in search.get("hits") or []:
            metadata = getattr(hit.get("document"), "metadata", None) or {}
            source = metadata.get("source")
            document_id = metadata.get("document_id") or (make_document_id(source) if source else None)
            chunk_id = metadata.get("chunk_id")
            if not document_id or not chunk_id:
                unresolved += 1
                continue
            if document_id not in doc_cache:
                # S3-2: column-accurate, currently-valid chunk->evidence map
                # (superseded rows can never shadow the current version).
                doc_cache[document_id] = store.current_evidence_by_chunk(
                    workspace_id=workspace, document_id=document_id
                )
            evidence = doc_cache[document_id].get(chunk_id)
            if evidence is None:
                unresolved += 1
                continue
            hits.append(
                {
                    "evidence_id": evidence["evidence_id"],
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "page": evidence.get("page"),
                    "source": source,
                    "score": hit.get("score"),
                    "snippet": hit.get("excerpt"),
                }
            )
            if len(hits) >= top_k:
                break
    finally:
        store.close()

    output = {
        "query": query,
        "hits": hits,
        "mock": False,
        "retrieval": "kb_keyword+store_resolve",
        "unresolved_count": unresolved,
    }
    if fallback_reason:
        output["hybrid_fallback_reason"] = fallback_reason
    latency = int((time.monotonic() - started) * 1000)
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output=output,
        metrics=ToolMetrics(latency_ms=latency, token_count=0),
        provenance=ctx.provenance,
    )
