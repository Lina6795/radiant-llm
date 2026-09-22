"""Shared fixtures for M9 API tests (offline, session-tmp DBs).

Importing ``api`` initialises the global chatbot (slow) and builds the M9
RunRuntime lazily on first use, so we point every control-plane DB at a
session-temporary directory BEFORE the first request, and share one
TestClient across the whole session.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="radiant-m9-tests-"))
os.environ.setdefault("RADIANT_DURABLE_DB", str(_SESSION_TMP / "durable.db"))
os.environ.setdefault("RADIANT_REVIEW_DB", str(_SESSION_TMP / "review_queue.db"))
os.environ.setdefault("RADIANT_EVIDENCE_DB", str(_SESSION_TMP / "evidence.db"))

# `import api` initialises the global chatbot, which requires the same keys the
# service gets from Docker_Executable/.env (sourced by start_radiant.sh).
_ENV_FILE = Path(__file__).resolve().parents[2] / "Docker_Executable" / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip())

import api  # noqa: E402  (slow import: builds the global chatbot once)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture(scope="session")
def runtime() -> api.RunRuntime:
    return api.get_run_runtime()


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.1, desc: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {desc}")


def wait_run_state(client, run_id: str, states: set[str], timeout: float = 15.0) -> dict:
    def check():
        resp = client.get(f"/runs/{run_id}")
        if resp.status_code == 404:
            return None  # background thread has not created the run record yet
        assert resp.status_code == 200
        snap = resp.json()
        return snap if snap["state"] in states else None

    return wait_for(check, timeout=timeout, desc=f"run {run_id} in {sorted(states)}")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
