"""S4-5: production context.assemble tool.

Deterministic (no LLM): evidence records in -> ContextPackage out, with the
full budget trace. This is the integration point the V3 S4 gate refers to:
any future answering model call consumes this package -- and never runs
without its budget trace present.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from app.control.models import ToolError, ToolMetrics, ToolResult, ToolStatus
from app.context.budgets import BudgetConfig
from app.context.engine import ContextEngine
from app.context.package import package_from_assembled
from app.context.quota import apply_source_quota, dedup_items, expand_neighbors
from app.context.selector import EvidenceItem
from app.evidence.store import get_evidence_store

TOOL_VERSION = "context.assemble/1"


def _terminal(code: str, message: str, started: float, provenance) -> ToolResult:
    return ToolResult(
        status=ToolStatus.TERMINAL_ERROR,
        error=ToolError(code=code, message=message, retryable=False),
        metrics=ToolMetrics(latency_ms=int((time.monotonic() - started) * 1000), token_count=0),
        provenance=provenance,
    )


def _to_item(record: Dict[str, Any], pinned: bool) -> EvidenceItem:
    span = record.get("source_span") or {}
    return EvidenceItem(
        evidence_id=record["evidence_id"],
        content=record.get("content") or "",
        page=record.get("page"),
        document_id=record.get("document_id"),
        chunk_id=span.get("chunk_id"),
        modality=record.get("modality") or "text",
        authority_level=record.get("authority_level") or "primary",
        pinned=pinned,
    )


def context_assemble_handler(arguments: Dict[str, Any], ctx) -> ToolResult:
    started = time.monotonic()
    records = arguments.get("evidence_records")
    if not isinstance(records, list) or any(not isinstance(r, dict) or "evidence_id" not in r for r in records):
        return _terminal(
            "context.invalid_input",
            "evidence_records must be a list of evidence records each carrying evidence_id",
            started, ctx.provenance,
        )
    question = arguments.get("question") or ""
    workspace = arguments.get("workspace_id") or ctx.workspace
    pinned_ids = set(arguments.get("pinned_evidence_ids") or [])

    config = BudgetConfig()
    budget_override = arguments.get("evidence_budget_tokens")
    if budget_override:
        config.quotas["evidence"] = int(budget_override)

    counter = None
    engine = ContextEngine(config=config)
    counter = engine.counter

    prep: Dict[str, Any] = {}
    items = [_to_item(r, r["evidence_id"] in pinned_ids) for r in records]
    items, dedup_dropped = dedup_items(items)
    prep["dedup_dropped"] = dedup_dropped
    items, quota_dropped = apply_source_quota(
        items, config.quotas["evidence"], counter=counter
    )
    prep["source_quota_dropped"] = quota_dropped

    neighbor_notes = []
    if items:
        store = get_evidence_store()
        try:
            by_chunk = store.current_evidence_by_chunk(workspace_id=workspace)
        finally:
            store.close()
        index = {
            chunk_id: _to_item(payload, False)
            for chunk_id, payload in by_chunk.items()
            if payload.get("evidence_id")
        }
        items, neighbor_notes = expand_neighbors(items, index, max_per_item=1)
    prep["neighbor_notes"] = neighbor_notes

    # S5: governed memory feed -- Read Gate (workspace/namespace/TTL/relevance)
    # is the only path memories may take into the context package.
    memory_lines: list[str] = []
    if arguments.get("include_memory", True):
        from app.memory.read_gate import ReadGate, ReadQuery
        from app.memory.store import MemoryStore, default_db_path as _memory_db_path

        mem_store = MemoryStore(_memory_db_path())
        try:
            recalled = ReadGate().recall(
                mem_store,
                ReadQuery(workspace=workspace, text=question, top_k=5),
            )
        finally:
            mem_store.close()
        memory_lines = [f"[mem:{r.memory_id}] {r.subject}: {r.value}" for r in recalled]
    prep["memory_recalled"] = len(memory_lines)

    assembled = engine.assemble(system="", active_turn=question, memory=memory_lines, evidence=items)
    package = package_from_assembled(assembled, config, counter_backend=counter.backend)
    # prep-stage drops must be visible in the SAME package trace as engine drops
    prep_drops = (
        [
            {"partition": "evidence", "item_id": d["evidence_id"], "reason": d["reason"],
             "tokens": d.get("tokens", 0)}
            for d in dedup_dropped
        ]
        + [
            {"partition": "evidence", "item_id": d["evidence_id"], "reason": d["reason"],
             "tokens": d.get("tokens", 0)}
            for d in quota_dropped
        ]
    )
    package.drops = prep_drops + list(package.drops)

    latency = int((time.monotonic() - started) * 1000)
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={
            "context_package": package.model_dump(),
            "prep": prep,
            "mock": False,
        },
        metrics=ToolMetrics(latency_ms=latency, token_count=0),
        provenance=ctx.provenance,
    )
