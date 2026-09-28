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
os.environ.setdefault("RADIANT_MEMORY_DB", str(_SESSION_TMP / "memory.db"))

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

# S1-5: production wiring uses the real evidence.search handler, which needs a
# KB directory; give the test runtime a tiny session-scoped KB whose single
# chunk is resolvable to a real evidence record in the session evidence DB,
# so the real search -> inspect chain (S1-7C) can complete end to end.
_KB_DIR = _SESSION_TMP / "kb"
_KB_DIR.mkdir(exist_ok=True)
with (_KB_DIR / "01_chunks_kb.jsonl").open("w", encoding="utf-8") as _fh:
    _fh.write(json.dumps({
        "source": "fixture.pdf", "page": 1, "chunk_index": 0,
        "chunk_id": "fixture:p1:c0",
        "content": "Reactor safety evidence report fixture content for api tests.",
    }) + "\n")
os.environ.setdefault("RADIANT_EVIDENCE_KB_DIR", str(_KB_DIR))

# Seed the matching evidence row (workspace "default") so evidence.search
# resolves chunk "fixture:p1:c0" to a real evidence_id and evidence.inspect
# can fetch it back.
import hashlib as _hl
import sqlite3 as _sqlite3

_FIXTURE_DOC_ID = _hl.sha1(b"fixture.pdf").hexdigest()[:16]
_fix_db = _SESSION_TMP / "evidence.db"
_conn = _sqlite3.connect(str(_fix_db))
_conn.executescript(
    "CREATE TABLE IF NOT EXISTS evidence ("
    " evidence_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,"
    " document_id TEXT NOT NULL, document_version TEXT NOT NULL,"
    " content_hash TEXT NOT NULL, modality TEXT NOT NULL, page INTEGER,"
    " section TEXT, figure_id TEXT, degraded INTEGER NOT NULL DEFAULT 0,"
    " degraded_reason TEXT, authority_level TEXT, artifact_uri TEXT,"
    " parser_fingerprint TEXT NOT NULL, payload TEXT NOT NULL,"
    " valid_from TEXT NOT NULL, valid_to TEXT);"
)
_payload = {
    "evidence_id": "ev-fixture0000000000000001",
    "workspace_id": "default",
    "document_id": _FIXTURE_DOC_ID,
    "document_version": "v-fixture",
    "modality": "text",
    "page": 1,
    "source_span": {"chunk_id": "fixture:p1:c0", "page": 1},
    "content": "Reactor safety evidence report fixture content for api tests.",
    "valid_from": "2026-09-24T00:00:00",
    "valid_to": None,
}
_conn.execute(
    "INSERT OR REPLACE INTO evidence (evidence_id, workspace_id, document_id,"
    " document_version, content_hash, modality, page, section, figure_id,"
    " degraded, degraded_reason, authority_level, artifact_uri,"
    " parser_fingerprint, payload, valid_from, valid_to)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (_payload["evidence_id"], "default", _FIXTURE_DOC_ID, "v-fixture", "hash-fixture",
     "text", 1, None, None, 0, None, None, None, "fp-fixture",
     json.dumps(_payload), _payload["valid_from"], None),
)
_conn.commit()
_conn.close()

import api  # noqa: E402  (slow import: builds the global chatbot once)
from fastapi.testclient import TestClient  # noqa: E402

# S6: stub the answer LLM for API tests (explicit test fixture; production
# resolves the real DeepSeek endpoint). Reply is aligned with the seeded
# fixture evidence so claim verification accepts deterministically.
from app.verification import answer_tools as _answer_tools  # noqa: E402


def _stub_chat(messages, **kw):
    return {
        "content": ("The fixture describes reactor safety evidence report "
                    "fixture content [ev-fixture0000000000000001]."),
        "latency_ms": 1, "prompt_tokens": 10, "completion_tokens": 5,
    }


_answer_tools._chat = _stub_chat


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
