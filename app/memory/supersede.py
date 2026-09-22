"""RADIANT-Control M6: supersede semantics.

Supersede is the *only* sanctioned way to replace a memory value (e.g. the
user corrects an old preference). It never overwrites history: the new
candidate must still pass the Write Gate, then the old record is flagged
``superseded_by`` and kept in the store for audit. Default recall excludes
superseded records; audit queries go through ``store.get`` /
``store.list_records(include_superseded=True)`` / ``store.audit_trail``.
"""

from __future__ import annotations

from app.memory.models import MemoryCandidate, MemoryRecord
from app.memory.write_gate import WriteDecision, WriteGate, WriteOutcome


def supersede(
    store,
    gate: WriteGate,
    old_memory_id: str,
    candidate: MemoryCandidate,
    *,
    now: float | None = None,
    actor: str = "user",
) -> WriteDecision:
    """Replace ``old_memory_id`` with a new record built from ``candidate``.

    The candidate must target the same workspace+subject as the old record;
    conflict detection is skipped because the replacement is explicit, but
    every other Write Gate rule still applies (confirmation, provenance,
    injection, sensitivity, ...).
    """
    now = gate._now() if now is None else now
    old = store.get(old_memory_id)
    if candidate.workspace != old.workspace or candidate.subject != old.subject:
        return WriteDecision(
            outcome=WriteOutcome.REJECT,
            reasons=["supersede_target_mismatch"],
            conflicting_with=old.memory_id,
        )
    decision = gate.evaluate(candidate, store=store, now=now, skip_conflict=True)
    if not decision.allowed:
        return decision
    record = store.put(MemoryRecord.from_candidate(candidate, now=now), actor="memory.write_gate")
    store.mark_superseded(
        old.memory_id,
        record.memory_id,
        reason=f"superseded by {record.memory_id}: {candidate.write_reason}",
        actor=actor,
    )
    decision.record = record
    return decision
