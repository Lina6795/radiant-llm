"""S1-4: real evidence.inspect handler — batch lookup by Evidence ID.

Read-only, no LLM, no writes. Enforces workspace isolation at the tool
boundary: an evidence record whose workspace_id differs from the caller's
workspace is denied (fail closed), never silently returned. Supersede state
(valid_to) is column-accurate via EvidenceStore.get_evidence.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from app.control.models import ToolError, ToolMetrics, ToolResult, ToolStatus
from app.evidence.store import get_evidence_store

TOOL_VERSION = "evidence.inspect/2"


def _error(status: ToolStatus, code: str, message: str, started: float, provenance) -> ToolResult:
    return ToolResult(
        status=status,
        error=ToolError(code=code, message=message, retryable=False),
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000), token_count=0),
        provenance=provenance,
    )


def evidence_inspect_handler(arguments: Dict[str, Any], ctx) -> ToolResult:
    started = time.monotonic()
    evidence_ids = arguments["evidence_ids"]
    workspace = arguments.get("workspace_id") or ctx.workspace

    store = get_evidence_store()
    try:
        found = []
        not_found = []
        for evidence_id in evidence_ids:
            item = store.get_evidence(evidence_id)
            if item is None:
                not_found.append(evidence_id)
                continue
            if item.get("workspace_id") != workspace:
                store.close()
                return _error(
                    ToolStatus.DENIED,
                    "evidence.workspace_mismatch",
                    f"evidence {evidence_id} belongs to a different workspace",
                    started,
                    ctx.provenance,
                )
            found.append(item)
    finally:
        try:
            store.close()
        except Exception:
            pass

    latency = int((time.monotonic() - started) * 1000)
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={"evidence": found, "not_found": not_found, "mock": False},
        metrics=ToolMetrics(latency_ms=latency, token_count=0),
        provenance=ctx.provenance,
    )
