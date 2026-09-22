"""RADIANT-Control M3: typed errors for the durable runtime.

Every failure mode that crosses a durable-runtime boundary raises one of
these typed errors so callers (and tests) can distinguish illegal state
transitions, lease fencing, missing runs and config mismatches without
parsing message strings.
"""

from __future__ import annotations


class DurableError(Exception):
    """Base class for all durable-runtime errors."""


class IllegalTransitionError(DurableError):
    """Raised when a state-machine transition is not in the legal table."""

    def __init__(self, entity: str, from_state: object, to_state: object) -> None:
        self.entity = entity
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal {entity} transition: {from_state} -> {to_state}")


class RunNotFoundError(DurableError):
    """Raised when a run_id has no persisted run record."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run not found: {run_id}")


class LeaseError(DurableError):
    """Base class for lease errors."""


class LeaseConflictError(LeaseError):
    """Raised when acquiring a lease that a different live owner still holds."""

    def __init__(self, run_id: str, owner: str, holder: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self.holder = holder
        super().__init__(f"lease for run {run_id} is held by {holder}; {owner} cannot acquire it")


class LeaseFencingError(LeaseError):
    """Raised when a stale owner / fencing token tries to commit to a run.

    After a takeover, the previous owner's submissions are rejected with
    this error, guaranteeing a single writer per run at any time.
    """

    def __init__(self, run_id: str, owner: str, detail: str) -> None:
        self.run_id = run_id
        self.owner = owner
        super().__init__(f"fenced submission for run {run_id} by {owner}: {detail}")


class CheckpointMismatchError(DurableError):
    """Raised when resume is attempted with a different configuration than
    the one recorded at run start (plan or tool-registry fingerprint)."""

    def __init__(self, run_id: str, stored: str, requested: str) -> None:
        self.run_id = run_id
        self.stored = stored
        self.requested = requested
        super().__init__(
            f"config fingerprint mismatch for run {run_id}: stored={stored} requested={requested}"
        )
