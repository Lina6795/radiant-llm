"""Unified trace schema and layer adapters.

One :class:`TraceRecord` shape for every layer:

    run_id, layer, stage, case_id, ts, inputs_summary, outputs_summary,
    latency_ms, cost, config_fingerprint, artifact_uri, status, extra

Adapters map the three existing layer outputs into the schema:

* M4 retrieval trace (``artifacts/retrieval/.../cases/<cfg>/<case>.json``
  -- the per-stage ``trace`` dict produced by
  :class:`retrieval.pipeline.RetrievalPipeline`);
* M3 durable event stream (:class:`app.durable.events.Event` rows from
  the append-only EventStore);
* M5 context decision record (``ContextDecision.to_dict()`` from
  :class:`app.context.engine.ContextEngine`, also persisted per run in
  ``artifacts/context/.../metrics.json`` cells).

Local artifacts are the source of truth; any external tracing backend
(LangSmith/Langfuse) is optional analysis only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = "radiant-trace/v1"


@dataclass
class TraceRecord:
    run_id: str
    layer: str                       # control | durable | retrieval | context | memory | verification
    stage: str                       # layer-specific stage, e.g. "recall_bm25" / "node_retried"
    case_id: Optional[str] = None
    ts: Optional[float] = None       # epoch seconds when known
    inputs_summary: Optional[str] = None
    outputs_summary: Optional[str] = None
    latency_ms: Optional[float] = None
    cost: Optional[float] = None     # USD when measurable, else None
    config_fingerprint: Optional[str] = None
    artifact_uri: Optional[str] = None
    status: str = "ok"               # ok | error
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION}
        out.update(asdict(self))
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceRecord":
        payload = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**payload)


class TraceWriter:
    """Append-only JSONL writer for unified trace records."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, records: Iterable[TraceRecord]) -> int:
        n = 0
        with self.path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                n += 1
        return n


def read_traces(path: Path) -> List[TraceRecord]:
    return [
        TraceRecord.from_dict(json.loads(line))
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# M4 retrieval trace adapter
# ---------------------------------------------------------------------------

def from_retrieval_trace(
    trace: Dict[str, Any],
    *,
    run_id: str,
    case_id: Optional[str] = None,
    artifact_uri: Optional[str] = None,
) -> List[TraceRecord]:
    """Map one M4 pipeline trace (the ``trace`` block of a case artifact)
    into one record per pipeline stage plus a final-summary record."""
    records: List[TraceRecord] = []
    question = trace.get("question", "")
    cfg_fp = trace.get("config_fingerprint")
    stages = trace.get("stages", {})
    for stage_name, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        latency = stage.get("latency_ms")
        n_cand = stage.get("candidates")
        if isinstance(n_cand, list):
            n_cand = len(n_cand)
        summary_parts = []
        if stage.get("verdict"):
            summary_parts.append(f"verdict={stage['verdict']}")
        if n_cand is not None:
            summary_parts.append(f"candidates={n_cand}")
        if stage.get("candidates_out") is not None:
            summary_parts.append(f"out={stage['candidates_out']}")
        records.append(TraceRecord(
            run_id=run_id, layer="retrieval", stage=stage_name, case_id=case_id,
            inputs_summary=question[:200] if stage_name.startswith("recall") else None,
            outputs_summary="; ".join(summary_parts) or None,
            latency_ms=latency, config_fingerprint=cfg_fp,
            artifact_uri=artifact_uri,
            extra={k: v for k, v in stage.items()
                   if k not in ("latency_ms", "candidates", "top_scores")
                   and isinstance(v, (str, int, float, bool, type(None)))},
        ))
    final = trace.get("final", [])
    records.append(TraceRecord(
        run_id=run_id, layer="retrieval", stage="final", case_id=case_id,
        outputs_summary=f"n_final={len(final)}; "
                        f"grader_verdict={trace.get('grader_verdict')}",
        latency_ms=trace.get("total_latency_ms"),
        config_fingerprint=cfg_fp, artifact_uri=artifact_uri,
        extra={"config_name": trace.get("config_name"),
               "top_evidence": [e.get("evidence_id") for e in final[:3]]},
    ))
    return records


# ---------------------------------------------------------------------------
# M3 durable event stream adapter
# ---------------------------------------------------------------------------

_NODE_STAGES = {"node_started", "node_completed", "node_failed",
                "node_retried", "node_skipped"}


def from_durable_events(
    events: Iterable[Any],
    *,
    run_id: Optional[str] = None,
    case_id: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> List[TraceRecord]:
    """Map an M3 EventStore stream (Event objects or dicts) into records."""
    records: List[TraceRecord] = []
    for event in events:
        if isinstance(event, dict):
            e_run_id = event.get("run_id")
            e_type = event.get("type")
            e_type = getattr(e_type, "value", e_type)
            payload = event.get("payload", {}) or {}
            created = event.get("created_at")
            seq = event.get("seq")
        else:
            e_run_id = event.run_id
            e_type = event.type.value if hasattr(event.type, "value") else str(event.type)
            payload = event.payload or {}
            created = event.created_at
            seq = event.seq
        error_types = {"node_failed", "run_failed"}
        outputs = None
        if payload:
            keys = ("step_id", "classification", "status", "owner",
                    "from_checkpoints", "attempt")
            outputs = "; ".join(f"{k}={payload[k]}" for k in keys if k in payload) or None
        records.append(TraceRecord(
            run_id=run_id or e_run_id, layer="durable", stage=e_type,
            case_id=case_id or payload.get("case_id"),
            ts=created,
            inputs_summary=None,
            outputs_summary=outputs,
            config_fingerprint=config_fingerprint,
            status="error" if e_type in error_types else "ok",
            extra={"seq": seq,
                   **({"error": payload.get("error")} if payload.get("error") else {})},
        ))
    return records


# ---------------------------------------------------------------------------
# M5 context decision adapter
# ---------------------------------------------------------------------------

def from_context_decision(
    decision: Dict[str, Any],
    *,
    run_id: str,
    case_id: Optional[str] = None,
    latency_ms: Optional[float] = None,
    config_fingerprint: Optional[str] = None,
    artifact_uri: Optional[str] = None,
) -> TraceRecord:
    """Map one M5 ``ContextDecision.to_dict()`` into a single record."""
    usage = decision.get("usage", {})
    outputs = (
        f"decision={decision.get('decision')}; "
        f"drops={len(decision.get('drops', []))}; "
        f"compressions={len(decision.get('compressions', []))}; "
        f"pointers={len(decision.get('pointers', []))}; "
        f"total_used={usage.get('total_used')}"
    )
    return TraceRecord(
        run_id=run_id, layer="context", stage="assemble", case_id=case_id,
        inputs_summary=None, outputs_summary=outputs, latency_ms=latency_ms,
        config_fingerprint=config_fingerprint, artifact_uri=artifact_uri,
        status="ok" if decision.get("decision") != "abstain" else "error",
        extra={
            "reasons": decision.get("reasons", []),
            "over_quota_input": decision.get("over_quota_input"),
            "residual_overflow_tokens": decision.get("residual_overflow_tokens"),
            "compression_lineage": [
                {k: c.get(k) for k in ("input_hash", "output_hash", "method",
                                       "tokens_before", "tokens_after")}
                for c in decision.get("compressions", [])
            ],
        },
    )
