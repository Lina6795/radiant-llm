"""RADIANT-Control M6: memory governance data models (Pydantic v2).

Two tiers of objects:

* :class:`MemoryCandidate` -- the only thing a model (or importer) may
  produce. A candidate is a *proposal*; it can never be recalled and never
  lands in the store without passing the Write Gate.
* :class:`MemoryRecord` -- a persisted memory. Always carries ``provenance``
  and ``write_reason``; the Write Gate refuses to create one without both,
  so the ratio of stored records lacking provenance is 0 by construction.

Time fields are epoch seconds (float) so tests can drive everything with a
deterministic fake clock.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MemoryCategory(str, Enum):
    """The five governed memory categories."""

    SESSION = "session"  # short-lived task state/summary (session_store belongs here)
    USER_FACT = "user_fact"  # stable user fact, requires explicit human confirmation
    DECISION = "decision"  # project decision confirmed by user/reviewer
    EVIDENCE_POINTER = "evidence_pointer"  # pointer to app.evidence evidence_id only
    ARTIFACT_POINTER = "artifact_pointer"  # pointer to file/image/report + metadata


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PERSONAL = "personal"  # personal data: requires explicit confirmation
    SECRET = "secret"  # credentials/keys: never storable


class Origin(str, Enum):
    """Where the candidate's content came from."""

    USER = "user"  # typed or explicitly confirmed by the user
    MODEL = "model"  # proposed by the model from conversation
    IMPORT = "import"  # bulk import / migration
    SYSTEM = "system"  # runtime bookkeeping


class Provenance(BaseModel):
    """Source trail of a memory. At least one of source_run_id /
    source_session_id / user_confirmation_id must be set, otherwise the
    Write Gate rejects with ``missing_provenance``."""

    origin: Origin = Origin.MODEL
    source_run_id: Optional[str] = None
    source_session_id: Optional[str] = None
    user_confirmation_id: Optional[str] = None
    evidence_id: Optional[str] = None  # set when this memory points at app.evidence

    def has_source(self) -> bool:
        return any(
            [
                self.source_run_id,
                self.source_session_id,
                self.user_confirmation_id,
            ]
        )


class MemoryCandidate(BaseModel):
    """A proposed memory. Never written directly; Write Gate only."""

    category: MemoryCategory
    subject: str
    value: str
    namespace: str = "default"
    workspace: str = "default"
    provenance: Provenance
    write_reason: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    valid_from: Optional[float] = None
    valid_to: Optional[float] = None
    ttl_seconds: Optional[float] = Field(default=None, gt=0)
    user_confirmed: bool = False
    user_confirmation_id: Optional[str] = None

    @field_validator("subject", "write_reason")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v


class RecordStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # kept for audit, excluded from default recall


class MemoryRecord(BaseModel):
    """A persisted, governed memory."""

    memory_id: str = Field(default_factory=lambda: f"mem-{uuid.uuid4().hex[:16]}")
    category: MemoryCategory
    subject: str
    value: str
    namespace: str
    workspace: str
    provenance: Provenance
    write_reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: Sensitivity
    created_at: float
    valid_from: Optional[float] = None
    valid_to: Optional[float] = None
    ttl_seconds: Optional[float] = None
    superseded_by: Optional[str] = None
    status: RecordStatus = RecordStatus.ACTIVE

    @classmethod
    def from_candidate(cls, candidate: MemoryCandidate, *, now: float | None = None) -> "MemoryRecord":
        return cls(
            category=candidate.category,
            subject=candidate.subject,
            value=candidate.value,
            namespace=candidate.namespace,
            workspace=candidate.workspace,
            provenance=candidate.provenance,
            write_reason=candidate.write_reason,
            confidence=candidate.confidence,
            sensitivity=candidate.sensitivity,
            created_at=time.time() if now is None else now,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
            ttl_seconds=candidate.ttl_seconds,
        )

    def is_expired(self, now: float) -> bool:
        """Expired iff TTL elapsed since creation or valid_to has passed."""
        if self.ttl_seconds is not None and self.created_at + self.ttl_seconds <= now:
            return True
        if self.valid_to is not None and self.valid_to <= now:
            return True
        return False
