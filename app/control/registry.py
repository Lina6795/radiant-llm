"""RADIANT-Control M2: tool registry.

Only registered, versioned tools are visible to the control plane. Four mock
tools are implemented in-process; the 13 legacy tools from app/tools are
registered as metadata only -- they are never imported or instantiated here,
and the scheduler refuses to execute tools without a handler.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.control.models import (
    Provenance,
    Risk,
    ToolError,
    ToolMetrics,
    ToolResult,
    ToolSpec,
    ToolStatus,
)

MOCK_TOOL_VERSION = "0.1.0-mock"
LEGACY_TOOL_VERSION = "1.0.0-legacy"

_MOCK_DOCS = [
    {"doc_id": "doc-001", "title": "Gen-IV Reactor Safety Overview", "snippet": "mock evidence: passive safety systems"},
    {"doc_id": "doc-002", "title": "Neutron Cross-Section Tables", "snippet": "mock evidence: U-235 thermal fission"},
    {"doc_id": "doc-003", "title": "Shielding Calculation Methods", "snippet": "mock evidence: buildup factors"},
]


class IdempotencyLedger:
    """In-memory idempotency ledger for side-effecting tools.

    The first call with a given key performs the side effect and stores the
    output; repeat calls with the same key replay the stored output without
    performing the side effect again. `effect_count` exposes the real number
    of side effects for tests and audits.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self.effect_count = 0

    def execute_once(self, key: str, effect: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        if key in self._records:
            return dict(self._records[key]), True
        output = effect()
        self.effect_count += 1
        self._records[key] = dict(output)
        return dict(output), False


@dataclass
class ToolContext:
    run_id: str
    workspace: str
    ledger: IdempotencyLedger
    provenance: Provenance


# ---------------------------------------------------------------------------
# Mock tool implementations
# ---------------------------------------------------------------------------

def _evidence_search(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    top_k = arguments.get("top_k", 5)
    hits = _MOCK_DOCS[: min(top_k, len(_MOCK_DOCS))]
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={"query": arguments["query"], "hits": hits, "mock": True},
        metrics=ToolMetrics(latency_ms=1, token_count=64),
        provenance=ctx.provenance,
    )


def _evidence_inspect(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    doc_id = arguments["doc_id"]
    for doc in _MOCK_DOCS:
        if doc["doc_id"] == doc_id:
            return ToolResult(
                status=ToolStatus.SUCCESS,
                output={"document": doc, "mock": True},
                metrics=ToolMetrics(latency_ms=1, token_count=48),
                provenance=ctx.provenance,
            )
    return ToolResult(
        status=ToolStatus.TERMINAL_ERROR,
        error=ToolError(code="evidence.not_found", message=f"no document {doc_id}", retryable=False),
        metrics=ToolMetrics(latency_ms=1, token_count=8),
        provenance=ctx.provenance,
    )


def _citation_validate(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    known = {d["doc_id"] for d in _MOCK_DOCS}
    doc_ids = arguments["doc_ids"]
    unsupported = [d for d in doc_ids if d not in known]
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output={
            "claims_checked": len(arguments["claims"]),
            "doc_ids": doc_ids,
            "unsupported_doc_ids": unsupported,
            "all_supported": not unsupported,
            "mock": True,
        },
        metrics=ToolMetrics(latency_ms=1, token_count=32),
        provenance=ctx.provenance,
    )


def _report_export(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    key = arguments.get("idempotency_key")
    if not key:
        # Policy should have denied this already; fail closed at the tool too.
        return ToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(
                code="report.missing_idempotency_key",
                message="report.export requires an idempotency_key",
                retryable=False,
            ),
            metrics=ToolMetrics(latency_ms=0, token_count=0),
            provenance=ctx.provenance,
        )

    def _effect() -> dict[str, Any]:
        return {
            "artifact": f"report://{ctx.workspace}/{ctx.run_id}/{key}",
            "title": arguments["title"],
            "bytes": len(arguments["content"].encode("utf-8")),
            "mock": True,
        }

    output, replayed = ctx.ledger.execute_once(key, _effect)
    output["idempotent_replay"] = replayed
    return ToolResult(
        status=ToolStatus.SUCCESS,
        output=output,
        artifacts=[output["artifact"]],
        metrics=ToolMetrics(latency_ms=1, token_count=16),
        provenance=ctx.provenance,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self.ledger = IdempotencyLedger()

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def catalog(self) -> list[dict[str, Any]]:
        """Metadata-only view handed to planners. Never exposes handlers."""
        return [
            {
                "name": s.name,
                "version": s.version,
                "risk": s.risk.value,
                "high_risk": s.high_risk,
                "description": s.description,
                "arguments_schema": s.arguments_schema,
                "implemented": s.implemented,
            }
            for s in (self._specs[n] for n in self.names())
        ]

    def config_fingerprint(self) -> str:
        payload = json.dumps(self.catalog(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def provenance_for(self, name: str) -> Provenance:
        spec = self._specs[name]
        return Provenance(tool_version=spec.version, config_fingerprint=self.config_fingerprint())

    def invoke(self, name: str, arguments: dict[str, Any], run_id: str, workspace: str) -> ToolResult:
        spec = self._specs[name]
        provenance = self.provenance_for(name)
        if not spec.implemented or spec.handler is None:
            return ToolResult(
                status=ToolStatus.TERMINAL_ERROR,
                error=ToolError(
                    code="tool.not_implemented",
                    message=f"tool {name} is registered as metadata only (M2)",
                    retryable=False,
                ),
                metrics=ToolMetrics(latency_ms=0, token_count=0),
                provenance=provenance,
            )
        ctx = ToolContext(run_id=run_id, workspace=workspace, ledger=self.ledger, provenance=provenance)
        started = time.monotonic()
        try:
            return spec.handler(arguments, ctx)
        except Exception as exc:  # fail closed: tool exceptions never propagate raw
            latency = int((time.monotonic() - started) * 1000)
            return ToolResult(
                status=ToolStatus.TERMINAL_ERROR,
                error=ToolError(code="tool.execution_error", message=f"{type(exc).__name__}: {exc}", retryable=False),
                metrics=ToolMetrics(latency_ms=latency, token_count=0),
                provenance=provenance,
            )


# ---------------------------------------------------------------------------
# Default registry: 4 mocks + 13 legacy metadata entries
# ---------------------------------------------------------------------------

_OBJECT = "object"


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": _OBJECT, "properties": properties, "required": required, "additionalProperties": False}


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()

    mocks = [
        ToolSpec(
            name="evidence.search",
            version=MOCK_TOOL_VERSION,
            risk=Risk.READ_ONLY,
            description="Mock: search the evidence store.",
            arguments_schema=_schema(
                {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                ["query"],
            ),
            implemented=True,
            handler=_evidence_search,
        ),
        ToolSpec(
            name="evidence.inspect",
            version=MOCK_TOOL_VERSION,
            risk=Risk.READ_ONLY,
            description="Mock: inspect one evidence document.",
            arguments_schema=_schema({"doc_id": {"type": "string"}}, ["doc_id"]),
            implemented=True,
            handler=_evidence_inspect,
        ),
        ToolSpec(
            name="citation.validate",
            version=MOCK_TOOL_VERSION,
            risk=Risk.READ_ONLY,
            description="Mock: validate claims against cited documents.",
            arguments_schema=_schema(
                {
                    "claims": {"type": "array", "items": {"type": "string"}},
                    "doc_ids": {"type": "array", "items": {"type": "string"}},
                },
                ["claims", "doc_ids"],
            ),
            implemented=True,
            handler=_citation_validate,
        ),
        ToolSpec(
            name="report.export",
            version=MOCK_TOOL_VERSION,
            risk=Risk.BOUNDED_WRITE,
            description="Mock: export a report artifact. Idempotent via idempotency_key.",
            arguments_schema=_schema(
                {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "idempotency_key": {"type": ["string", "null"]},
                },
                ["title", "content"],
            ),
            implemented=True,
            handler=_report_export,
        ),
    ]

    legacy = [
        ("PDFReaderTool", Risk.READ_ONLY, False),
        ("PDFKnowledgeBaseSanitizerTool", Risk.BOUNDED_WRITE, False),
        ("URLValidationTool", Risk.READ_ONLY, False),
        ("WebSearchTool", Risk.EXTERNAL, True),
        ("WebScraperTool", Risk.EXTERNAL, False),
        ("WikipediaSearchTool", Risk.EXTERNAL, False),
        ("PythonREPLTool", Risk.EXTERNAL, True),
        ("ImageAnalysisTool", Risk.READ_ONLY, False),
        ("CSVandExcelFileParserTool", Risk.READ_ONLY, False),
        ("CSVDataFinderTool", Risk.READ_ONLY, False),
        ("TextFileReaderTool", Risk.READ_ONLY, False),
        ("SkillLookupTool", Risk.READ_ONLY, False),
        ("FileDownloaderTool", Risk.EXTERNAL, True),
    ]
    legacy_specs = [
        ToolSpec(
            name=name,
            version=LEGACY_TOOL_VERSION,
            risk=risk,
            high_risk=high_risk,
            description="Legacy app/tools entry; metadata only in M2, not executable.",
            arguments_schema=_schema({}, []),
            implemented=False,
        )
        for name, risk, high_risk in legacy
    ]

    for spec in mocks + legacy_specs:
        reg.register(spec)
    return reg
