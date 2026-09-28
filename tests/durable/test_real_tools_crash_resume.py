"""S2-3: the REAL evidence.search / evidence.inspect adapters execute under the
DurableRunner; a crash after search succeeds must not re-run search, and the
resumed inspect receives its arguments from the persisted checkpoint output.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.control.registry import build_default_registry
from app.durable.events import EventType
from app.durable.graph import RunState
from app.durable.runner import DurableRunner

from tests.durable.conftest import Stores, SimulatedCrash, make_backoff, make_plan, make_step, reopen_stores

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "artifacts" / "baseline" / "m0-20260922"

pytestmark = pytest.mark.skipif(
    not (BASELINE / "evidence.db").exists() or not (BASELINE / "output").exists(),
    reason="baseline ingestion artifacts absent",
)

REF = {"ref": "step_output", "from_step": "s1-search", "path": "output.hits[*].evidence_id", "expects": "array<string>"}
QUERY = ("What two main components does the Transformer architecture consist of? "
         "Answer with the PDF name, page number and Evidence ID.")

@pytest.fixture(scope="module")
def real_env():
    """Point the real adapters at the baseline KB + evidence DB.

    Self-contained: no env inheritance from any live service process.
    The lazy legacy import (utils.pdf_helpers -> utils.general_utilities)
    writes os.environ["OPENAI_API_KEY"]/["LANGCHAIN_API_KEY"] at module
    import time and crashes on None, so placeholders are set only when the
    key is absent. The search/inspect paths exercised here are JSONL/file
    based and never call an LLM API at runtime.
    """
    os.environ.setdefault("OPENAI_API_KEY", "pytest-placeholder-not-used")
    os.environ.setdefault("LANGCHAIN_API_KEY", "")
    os.environ["RADIANT_EVIDENCE_KB_DIR"] = str(BASELINE / "output")
    os.environ["RADIANT_EVIDENCE_DB"] = str(BASELINE / "evidence.db")
    os.environ["RADIANT_EVIDENCE_SEARCH_REAL"] = "1"
    os.environ["RADIANT_EVIDENCE_INSPECT_REAL"] = "1"
    # Warm the lazy legacy import outside any node timeout budget.
    from utils.pdf_helpers import direct_jsonl_kb_search
    direct_jsonl_kb_search(working_directory=os.environ["RADIANT_EVIDENCE_KB_DIR"], query="warmup", max_hits=1)


def _counting_registry(calls: list, received: list, crash_first_inspect: bool):
    registry = build_default_registry(evidence="real")
    state = {"crashed": False}

    search_spec = registry.get("evidence.search")
    real_search = search_spec.handler
    inspect_spec = registry.get("evidence.inspect")
    real_inspect = inspect_spec.handler

    def counting_search(arguments, ctx):
        calls.append("s1-search")
        return real_search(arguments, ctx)

    def crashing_inspect(arguments, ctx):
        if crash_first_inspect and not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("kill inside real inspect after real search succeeded")
        calls.append("s2-inspect")
        received.append(dict(arguments))
        return real_inspect(arguments, ctx)

    search_spec.handler = counting_search
    inspect_spec.handler = crashing_inspect
    return registry


def _runner(stores: Stores, registry) -> DurableRunner:
    return DurableRunner(
        registry=registry, checkpoints=stores.checkpoints, events=stores.events,
        leases=stores.leases, ledger=stores.ledger, backoff=make_backoff(stores.clock),
    )


def _plan():
    return make_plan(
        [
            make_step("s1-search", "evidence.search", arguments={"query": QUERY, "top_k": 3}),
            make_step("s2-inspect", "evidence.inspect", depends_on=["s1-search"], arguments={"evidence_ids": REF}),
        ]
    )


def test_real_tools_crash_resume_does_not_rerun_search(tmp_path, real_env) -> None:
    stores = Stores(tmp_path / "d.db")
    calls: list = []
    received: list = []
    plan = _plan()
    runner = _runner(stores, _counting_registry(calls, received, crash_first_inspect=True))
    with pytest.raises(SimulatedCrash):
        runner.run(plan, workspace="default")
    assert calls == ["s1-search"]  # inspect crashed before completing

    # Restart: new store instances over the same SQLite file, no in-memory plan.
    stores2 = reopen_stores(stores)
    report = _runner(stores2, _counting_registry(calls, received, crash_first_inspect=False)).resume(
        run_id=str(plan.run_id), force_takeover=True
    )
    assert report.status == RunState.SUCCEEDED
    assert calls == ["s1-search", "s2-inspect"]  # real search NOT re-executed

    # The resumed inspect received evidence_ids resolved from the checkpointed
    # real search output -- IDs that must exist in the real evidence DB.
    assert len(received) == 1
    resolved = received[0]["evidence_ids"]
    assert resolved and all(isinstance(e, str) and e.startswith("ev-") for e in resolved)
    result = report.results["s2-inspect"]
    found = {e["evidence_id"] for e in result.output.get("evidence", [])}
    assert set(resolved) == found
    assert result.output.get("mock") is False

    binding = [e for e in stores2.events.stream(str(plan.run_id)) if e.type == EventType.BINDING_RESOLVED]
    # two resolutions: the crashed attempt resolved before the kill, the
    # resumed attempt re-resolved from the same immutable checkpoint.
    assert len(binding) == 2
    assert all(e.payload["count"] == len(resolved) for e in binding)
    assert binding[0].payload["digest"] == binding[1].payload["digest"]
