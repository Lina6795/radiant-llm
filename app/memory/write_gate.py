"""RADIANT-Control M6: Write Gate.

Every long-term memory write is a :class:`MemoryCandidate` evaluated here.
The gate is fail-closed: any rule violation yields ``REJECT`` (with all
machine-readable reason codes); a same-subject different-value collision
yields a conflict decision (``CONFLICT_CLARIFY`` when unconfirmed,
``CONFLICT_REVIEW`` when the user confirmed it) and never overwrites.

Rules, in evaluation order (all reject reasons are collected):

1. ``missing_provenance`` -- no run/session/confirmation source at all.
2. ``readonly_namespace`` -- skills/policy namespaces are read-only policy
   context; any write is refused.
3. ``evidence_as_user_fact`` -- external evidence content may never be
   converted into a user_fact (pointer category only).
4. ``evidence_pointer_missing_target`` -- an evidence_pointer must carry an
   evidence_id to point at.
5. ``low_confidence`` -- below the gate threshold.
6. ``secret_detected`` -- credential/key pattern matched; never storable.
7. ``personal_requires_confirmation`` -- personal-data pattern matched and
   the user has not explicitly confirmed.
8. ``prompt_injection`` -- injection phrase (CN/EN) in an unconfirmed
   candidate.
9. ``missing_user_confirmation`` -- user_fact/decision require an explicit
   confirmation flag plus confirmation id.
10. ``duplicate_value`` -- identical active record already exists.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.memory.models import (
    MemoryCandidate,
    MemoryCategory,
    MemoryRecord,
    Sensitivity,
)

READONLY_NAMESPACES = frozenset({"skills", "skill_library", "policy"})

DEFAULT_MIN_CONFIDENCE = 0.5

_CATEGORIES_REQUIRING_CONFIRMATION = frozenset(
    {MemoryCategory.USER_FACT, MemoryCategory.DECISION}
)

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|access[_-]?token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"),
]

_PERSONAL_PATTERNS = [
    re.compile(r"\b1[3-9]\d{9}\b"),  # CN mobile
    re.compile(r"\b\d{17}[\dXx]\b"),  # CN national id
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),  # email
]

_INJECTION_PATTERNS = [
    re.compile(r"记住[:：]"),
    re.compile(r"写入长期记忆"),
    re.compile(r"加入长期记忆"),
    re.compile(r"存入记忆"),
    re.compile(r"忽略(之前|以前|上述)的?(所有|全部)?指令"),
    re.compile(r"(?i)\bsystem\s*[:：]"),
    re.compile(r"(?i)<\s*/?\s*system\s*>"),
    re.compile(r"(?i)\bignore (all |any )?(previous|prior|above) instructions\b"),
    re.compile(r"(?i)\bremember (this|it)\s*[:：]"),
    re.compile(r"(?i)\bwrite (this )?to (long[- ]term )?memory\b"),
    re.compile(r"(?i)\boverride (the )?memory\b"),
]


class WriteOutcome(str, Enum):
    ALLOW = "allow"
    REJECT = "reject"
    CONFLICT_CLARIFY = "conflict_clarify"  # unconfirmed conflicting value: ask the user
    CONFLICT_REVIEW = "conflict_review"  # confirmed conflicting value: human review / explicit supersede


class WriteDecision(BaseModel):
    outcome: WriteOutcome
    reasons: list[str] = []
    record: Optional[MemoryRecord] = None
    conflicting_with: Optional[str] = None  # memory_id of the existing active record

    @property
    def allowed(self) -> bool:
        return self.outcome == WriteOutcome.ALLOW


def detect_sensitivity(text: str) -> Optional[Sensitivity]:
    """Scan text for secret/personal patterns. Returns the highest detected
    sensitivity, or None when nothing matches."""
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return Sensitivity.SECRET
    if any(p.search(text) for p in _PERSONAL_PATTERNS):
        return Sensitivity.PERSONAL
    return None


def detect_injection(text: str) -> bool:
    return any(p.search(text) for p in _INJECTION_PATTERNS)


class WriteGate:
    def __init__(
        self,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        readonly_namespaces: frozenset[str] = READONLY_NAMESPACES,
        now=None,
    ) -> None:
        self._min_confidence = min_confidence
        self._readonly = readonly_namespaces
        self._now = now or time.time

    def evaluate(
        self,
        candidate: MemoryCandidate,
        *,
        store=None,
        now: float | None = None,
        skip_conflict: bool = False,
    ) -> WriteDecision:
        """Fail-closed evaluation. ``store`` (optional MemoryStore) enables
        duplicate/conflict detection; ``skip_conflict`` is used only by the
        explicit supersede path."""
        now = self._now() if now is None else now
        reasons: list[str] = []

        if not candidate.provenance.has_source():
            reasons.append("missing_provenance")

        if not candidate.write_reason or not candidate.write_reason.strip():
            reasons.append("missing_write_reason")

        if candidate.namespace in self._readonly:
            reasons.append("readonly_namespace")

        if (
            candidate.category == MemoryCategory.USER_FACT
            and candidate.provenance.evidence_id
        ):
            reasons.append("evidence_as_user_fact")

        if (
            candidate.category == MemoryCategory.EVIDENCE_POINTER
            and not candidate.provenance.evidence_id
        ):
            reasons.append("evidence_pointer_missing_target")

        if candidate.confidence < self._min_confidence:
            reasons.append("low_confidence")

        text = f"{candidate.subject}\n{candidate.value}"
        detected = detect_sensitivity(text)
        if detected == Sensitivity.SECRET:
            reasons.append("secret_detected")
        elif detected == Sensitivity.PERSONAL and not candidate.user_confirmed:
            reasons.append("personal_requires_confirmation")

        if detect_injection(text) and not candidate.user_confirmed:
            reasons.append("prompt_injection")

        confirmed = candidate.user_confirmed and bool(candidate.user_confirmation_id)
        if candidate.category in _CATEGORIES_REQUIRING_CONFIRMATION and not confirmed:
            reasons.append("missing_user_confirmation")

        if reasons:
            return WriteDecision(outcome=WriteOutcome.REJECT, reasons=reasons)

        if store is not None and not skip_conflict:
            existing = store.find_active(
                workspace=candidate.workspace,
                category=candidate.category.value,
                subject=candidate.subject,
            )
            for rec in existing:
                if rec.is_expired(now):
                    continue
                if rec.value == candidate.value:
                    return WriteDecision(
                        outcome=WriteOutcome.REJECT,
                        reasons=["duplicate_value"],
                        conflicting_with=rec.memory_id,
                    )
                outcome = (
                    WriteOutcome.CONFLICT_REVIEW if confirmed else WriteOutcome.CONFLICT_CLARIFY
                )
                return WriteDecision(
                    outcome=outcome,
                    reasons=["conflict_same_subject_different_value"],
                    conflicting_with=rec.memory_id,
                )

        return WriteDecision(outcome=WriteOutcome.ALLOW)


def attempt_write(
    store,
    gate: WriteGate,
    candidate: MemoryCandidate,
    *,
    now: float | None = None,
    actor: str = "memory.write_gate",
) -> WriteDecision:
    """Evaluate and, iff ALLOW, persist. Returns the decision (with the
    created record attached when allowed)."""
    now = gate._now() if now is None else now
    decision = gate.evaluate(candidate, store=store, now=now)
    if decision.allowed:
        decision.record = store.put(MemoryRecord.from_candidate(candidate, now=now), actor=actor)
    return decision
