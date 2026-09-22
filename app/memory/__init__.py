"""RADIANT-Control M6: memory governance library.

A standalone, offline governance layer for long-term memory. Models may only
propose :class:`MemoryCandidate`; every write must pass the Write Gate, every
recall must pass the Read Gate, and history is preserved via supersede
(never overwrite) plus an audit trail.

This package deliberately does NOT touch ``app.radiant_llm`` or
``app.utils.session_store`` -- wiring into the runtime is a separate step.
"""

from app.memory.models import (
    MemoryCandidate,
    MemoryCategory,
    MemoryRecord,
    Origin,
    Provenance,
    RecordStatus,
    Sensitivity,
)
from app.memory.read_gate import ReadGate, ReadQuery
from app.memory.store import MemoryStore, MemoryStoreError
from app.memory.supersede import supersede
from app.memory.write_gate import (
    READONLY_NAMESPACES,
    WriteDecision,
    WriteGate,
    WriteOutcome,
    attempt_write,
    detect_sensitivity,
)

__all__ = [
    "MemoryCandidate",
    "MemoryCategory",
    "MemoryRecord",
    "MemoryStore",
    "MemoryStoreError",
    "Origin",
    "Provenance",
    "READONLY_NAMESPACES",
    "ReadGate",
    "ReadQuery",
    "RecordStatus",
    "Sensitivity",
    "WriteDecision",
    "WriteGate",
    "WriteOutcome",
    "attempt_write",
    "detect_sensitivity",
    "supersede",
]
