"""S4-1: production ContextPackage contract.

Freezes what the context.assemble tool emits and what any future LLM call
must receive BEFORE it runs: per-partition budgets and usage, items with
lineage, drop/compression decisions, and a stable fingerprint. The single
token-counting entry stays ``app.context.tokenizer`` (the engine's counter);
nothing in production may fall back to chars//4 heuristics.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .budgets import PARTITIONS, BudgetConfig
from .engine import AssembledContext


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    partition: str
    tokens: int = Field(ge=0)
    pinned: bool = False
    evidence_id: Optional[str] = None
    document_id: Optional[str] = None
    page: Optional[int] = None
    chunk_id: Optional[str] = None
    modality: Optional[str] = None
    source_kind: str = "inline"  # inline | artifact_pointer
    ref: Optional[str] = None    # artifact pointer uri when spilled
    neighbor_of: Optional[str] = None


class ContextPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_fingerprint: str
    token_counter: str
    budgets: Dict[str, int]
    usage: Dict[str, int]
    items: List[ContextItem] = Field(default_factory=list)
    decision: str
    reasons: List[str] = Field(default_factory=list)
    drops: List[Dict[str, Any]] = Field(default_factory=list)
    compressions: List[Dict[str, Any]] = Field(default_factory=list)
    artifact_pointers: List[Dict[str, Any]] = Field(default_factory=list)
    over_quota_input: bool = False
    # The assembled context text itself: what answer.draft feeds the model.
    text: Optional[str] = None


def package_from_assembled(assembled: AssembledContext, config: BudgetConfig,
                           counter_backend: str = "heuristic") -> ContextPackage:
    """Project the engine's AssembledContext into the frozen contract.
    ``counter_backend`` is the engine's TokenCounter.backend (single counting
    entry -- never re-derive counts here)."""
    decision = assembled.decision
    items: List[ContextItem] = []
    for it in assembled.evidence:
        items.append(
            ContextItem(
                item_id=f"evidence:{it.evidence_id}",
                partition="evidence",
                tokens=it.tokens(),
                pinned=it.pinned,
                evidence_id=it.evidence_id,
                document_id=it.document_id,
                page=it.page,
                chunk_id=it.chunk_id,
                modality=it.modality,
                neighbor_of=it.neighbor_of,
            )
        )
    for name in PARTITIONS:
        if name == "evidence":
            continue
        text = assembled.partitions.get(name) or ""
        if not text:
            continue
        items.append(ContextItem(item_id=f"{name}:0", partition=name, tokens=0))
    # token counts for non-evidence partitions come from decision.usage
    usage = dict(decision.usage or {})
    for it in items:
        if it.partition != "evidence":
            it.tokens = int(usage.get(it.partition, 0))

    payload = {
        "partitions": assembled.partitions,
        "budgets": dict(config.quotas),
        "decision": decision.decision,
        "evidence_ids": [it.evidence_id for it in assembled.evidence],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]

    return ContextPackage(
        package_fingerprint=fingerprint,
        token_counter=counter_backend,
        budgets={name: config.quotas.get(name, 0) for name in PARTITIONS},
        usage=usage,
        items=items,
        decision=decision.decision,
        reasons=list(decision.reasons),
        drops=[d.to_dict() for d in decision.drops],
        compressions=[c.to_dict() for c in decision.compressions],
        artifact_pointers=[p.to_dict() for p in decision.pointers],
        over_quota_input=bool(decision.over_quota_input),
        text=assembled.text,
    )
