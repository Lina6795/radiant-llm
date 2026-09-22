"""RADIANT-Control M6: memory governance metrics.

All metrics are pure functions over plain Python data so they can be
computed offline in tests and, later, by the telemetry flywheel:

* ``write_precision`` -- of the writes the gate ALLOWED, the fraction that
  were legitimate. 1.0 means no attack/invalid candidate slipped through.
* ``recall_at_k`` -- fraction of relevant memories present in the top-K
  recall result.
* ``stale_hit_rate`` -- fraction of recalled records that were expired at
  recall time. Must be 0 for default recall.
* ``conflict_detection_recall`` -- fraction of true conflicts (same subject,
  different value) that produced a conflict decision.
* ``cross_workspace_leakage`` -- count of recalled records whose workspace
  differs from the query workspace. Must be 0.
* ``missing_provenance_ratio`` -- fraction of stored records missing
  provenance source or write_reason. Must be 0 (the Write Gate enforces it).
"""

from __future__ import annotations

from typing import Iterable, Sequence

from app.memory.models import MemoryRecord


def write_precision(allowed: Sequence[bool], legitimate: Sequence[bool]) -> float:
    """allowed[i]: gate allowed candidate i; legitimate[i]: candidate i was
    a genuinely valid write. Precision = legit-and-allowed / allowed."""
    if len(allowed) != len(legitimate):
        raise ValueError("allowed and legitimate must have equal length")
    total_allowed = sum(1 for a in allowed if a)
    if total_allowed == 0:
        return 1.0
    good = sum(1 for a, l in zip(allowed, legitimate) if a and l)
    return good / total_allowed


def recall_at_k(retrieved_ids: Iterable[str], relevant_ids: Iterable[str]) -> float:
    relevant = set(relevant_ids)
    if not relevant:
        return 1.0
    retrieved = set(retrieved_ids)
    return len(retrieved & relevant) / len(relevant)


def stale_hit_rate(records: Sequence[MemoryRecord], now: float) -> float:
    if not records:
        return 0.0
    stale = sum(1 for r in records if r.is_expired(now))
    return stale / len(records)


def conflict_detection_recall(flagged: Sequence[bool], is_conflict: Sequence[bool]) -> float:
    if len(flagged) != len(is_conflict):
        raise ValueError("flagged and is_conflict must have equal length")
    total = sum(1 for c in is_conflict if c)
    if total == 0:
        return 1.0
    detected = sum(1 for f, c in zip(flagged, is_conflict) if f and c)
    return detected / total


def cross_workspace_leakage(records: Sequence[MemoryRecord], workspace: str) -> int:
    return sum(1 for r in records if r.workspace != workspace)


def missing_provenance_ratio(records: Sequence[MemoryRecord]) -> float:
    if not records:
        return 0.0
    missing = sum(
        1
        for r in records
        if not r.provenance.has_source() or not r.write_reason.strip()
    )
    return missing / len(records)
