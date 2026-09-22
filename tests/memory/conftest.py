"""Shared helpers for memory-governance tests (offline, tmp dirs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.memory import MemoryStore, ReadGate, WriteGate
from app.memory.models import MemoryCandidate, MemoryCategory, Origin, Provenance

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"


def load_cases(filename: str) -> list[dict]:
    path = BENCHMARKS_DIR / filename
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def case_ids(cases: list[dict]) -> list[str]:
    return [c["case_id"] for c in cases]


class FakeClock:
    """Deterministic clock: advance() moves virtual time forward."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def make_candidate(
    *,
    category: MemoryCategory = MemoryCategory.USER_FACT,
    subject: str = "偏好编辑器",
    value: str = "vim",
    namespace: str = "default",
    workspace: str = "ws-a",
    confidence: float = 0.9,
    origin: Origin = Origin.USER,
    source_run_id: str | None = "run-1",
    source_session_id: str | None = "sess-1",
    user_confirmation_id: str | None = "confirm-1",
    evidence_id: str | None = None,
    write_reason: str = "用户明确陈述并确认",
    user_confirmed: bool = True,
    ttl_seconds: float | None = None,
    valid_from: float | None = None,
    valid_to: float | None = None,
) -> MemoryCandidate:
    """Builds a candidate that passes the Write Gate by default; tests
    override individual fields to probe specific rules."""
    return MemoryCandidate(
        category=category,
        subject=subject,
        value=value,
        namespace=namespace,
        workspace=workspace,
        confidence=confidence,
        provenance=Provenance(
            origin=origin,
            source_run_id=source_run_id,
            source_session_id=source_session_id,
            user_confirmation_id=user_confirmation_id,
            evidence_id=evidence_id,
        ),
        write_reason=write_reason,
        user_confirmed=user_confirmed,
        user_confirmation_id=user_confirmation_id if user_confirmed else None,
        ttl_seconds=ttl_seconds,
        valid_from=valid_from,
        valid_to=valid_to,
    )


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def store(tmp_path: Path, clock: FakeClock) -> MemoryStore:
    s = MemoryStore(str(tmp_path / "memory.db"), now=clock.now)
    yield s
    s.close()


@pytest.fixture()
def gate(clock: FakeClock) -> WriteGate:
    return WriteGate(now=clock.now)


@pytest.fixture()
def read_gate(clock: FakeClock) -> ReadGate:
    return ReadGate(now=clock.now)
