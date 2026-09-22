"""Fault-injection scenario drivers for RADIANT-Control M3.

Single source of truth for the eight frozen failure scenarios. Each driver
builds its own stores under a caller-provided tmp dir, injects the fault, and
returns a detail dict; ``run_case`` then compares the detail against the
``expected`` block of a frozen benchmark case and computes ``recovered``.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app.control.models import RetryPolicy, Risk, ToolResult, ToolStatus
from app.control.registry import build_default_registry
from app.durable.errors import IllegalTransitionError, LeaseFencingError
from app.durable.events import EventType
from app.durable.graph import RunState, StateGraph, StepState, guard_run_transition
from app.durable.runner import DurableRunner

from tests.durable.conftest import (
    FakeClock,
    SimulatedCrash,
    Stores,
    counting_handler,
    flaky_handler,
    make_backoff,
    make_plan,
    make_step,
    register_test_tool,
    reopen_stores,
    spy_invocations,
    terminal_handler,
)


def _runner(stores: Stores, registry, **kwargs) -> DurableRunner:
    kwargs.setdefault("backoff", make_backoff(stores.clock))
    return DurableRunner(
        registry=registry,
        checkpoints=stores.checkpoints,
        events=stores.events,
        leases=stores.leases,
        ledger=stores.ledger,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# a) retrieval node fails once with retryable_error, then succeeds
# ---------------------------------------------------------------------------

def scenario_transient_retry(work_dir: Path) -> dict[str, Any]:
    stores = Stores(work_dir / "a.db")
    try:
        registry = build_default_registry()
        flaky_calls: list[str] = []
        register_test_tool(
            registry, "test.flaky", flaky_handler(flaky_calls, name="test.flaky", failures_before_success=1)
        )
        invocations = spy_invocations(registry)
        plan = make_plan(
            [
                make_step("search", "evidence.search", arguments={"query": "passive safety"}),
                make_step(
                    "retrieve", "test.flaky", depends_on=["search"], retry_policy=RetryPolicy.TRANSIENT_ONLY
                ),
                make_step(
                    "cite",
                    "citation.validate",
                    depends_on=["retrieve"],
                    arguments={"claims": ["c1"], "doc_ids": ["doc-001"]},
                ),
            ]
        )
        report = _runner(stores, registry).run(plan)
        types = [e.type.value for e in stores.events.stream(str(plan.run_id))]
        upstream_calls = sum(1 for i in invocations if i["tool"] == "evidence.search")
        return {
            "run_status": report.status.value,
            "retried": EventType.NODE_RETRIED.value in types,
            "flaky_calls": len(flaky_calls),
            "upstream_calls": upstream_calls,
        }
    finally:
        stores.close()


# ---------------------------------------------------------------------------
# b) LLM node fails permanently with terminal_error
# ---------------------------------------------------------------------------

def scenario_terminal_error(work_dir: Path) -> dict[str, Any]:
    stores = Stores(work_dir / "b.db")
    try:
        registry = build_default_registry()
        llm_calls: list[str] = []
        register_test_tool(registry, "test.llm", terminal_handler(llm_calls, name="test.llm"))
        plan = make_plan(
            [make_step("llm", "test.llm", retry_policy=RetryPolicy.TRANSIENT_ONLY)]
        )
        report = _runner(stores, registry).run(plan)
        events = stores.events.stream(str(plan.run_id))
        failed = next(e for e in events if e.type == EventType.NODE_FAILED)
        return {
            "run_status": report.status.value,
            "llm_calls": len(llm_calls),
            "retried": any(e.type == EventType.NODE_RETRIED for e in events),
            "classification": failed.payload.get("classification"),
        }
    finally:
        stores.close()


# ---------------------------------------------------------------------------
# c) report.export succeeded, then "the network goes down": any retry must
#    replay the first result without performing the side effect again
# ---------------------------------------------------------------------------

def scenario_export_then_disconnect(work_dir: Path) -> dict[str, Any]:
    """report.export succeeds (side effect #1 + checkpoint), then the worker
    "dies"; after a restart with the network down, the same idempotency key
    must replay the first result and resume must skip the export node without
    invoking the tool again."""
    stores = Stores(work_dir / "c.db")
    key = "k-export-netdown"
    registry = build_default_registry()
    invocations = spy_invocations(registry)
    post_calls: list[str] = []
    mode = {"crash": True}

    def crashy(arguments, ctx):
        post_calls.append("post")
        if mode["crash"]:
            raise SimulatedCrash()
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"ok": True},
            provenance=ctx.provenance,
        )

    register_test_tool(registry, "test.post", crashy)
    plan = make_plan(
        [
            make_step(
                "export",
                "report.export",
                arguments={"title": "T", "content": "body", "idempotency_key": key},
                risk=Risk.BOUNDED_WRITE,
                idempotency_key=key,
            ),
            make_step("post", "test.post", depends_on=["export"]),
        ]
    )

    crashed = False
    try:
        _runner(stores, registry).run(plan)
    except SimulatedCrash:
        crashed = True
    first_stored = stores.ledger.get(key)

    # Process restart + network outage: any fresh side effect would raise.
    stores2 = reopen_stores(stores)
    try:
        def poisoned_effect() -> dict[str, Any]:
            raise ConnectionError("simulated network outage")

        replayed_result, replayed = stores2.ledger.execute_once(key, poisoned_effect)
        mode["crash"] = False
        report2 = _runner(stores2, registry).resume(plan)
        skipped = any(
            e.type == EventType.NODE_SKIPPED and e.payload.get("step_id") == "export"
            for e in stores2.events.stream(str(plan.run_id))
        )
        export_tool_calls = sum(1 for i in invocations if i["tool"] == "report.export")
        return {
            "first_run_crashed": crashed,
            "resume_status": report2.status.value,
            "replay_returned_first": replayed and replayed_result == first_stored,
            "effect_count": stores2.ledger.effect_count_for(key),
            "skipped_on_resume": skipped,
            "export_tool_calls": export_tool_calls,
        }
    finally:
        stores2.close()


# ---------------------------------------------------------------------------
# d) worker dies after a checkpoint; resume from the exact checkpoint
# ---------------------------------------------------------------------------

def scenario_crash_resume(work_dir: Path) -> dict[str, Any]:
    stores = Stores(work_dir / "d.db")
    s1_calls: list[str] = []
    s2_calls: list[str] = []
    s3_calls: list[str] = []
    mode = {"crash": True}

    def crashy(arguments, ctx):
        s2_calls.append("s2")
        if mode["crash"]:
            raise SimulatedCrash()
        return ToolResult(
            status=ToolStatus.SUCCESS,
            output={"ok": True},
            provenance=ctx.provenance,
        )

    registry = build_default_registry()
    register_test_tool(registry, "test.s1", counting_handler(s1_calls, name="s1"))
    register_test_tool(registry, "test.s2", crashy)
    register_test_tool(registry, "test.s3", counting_handler(s3_calls, name="s3"))
    plan = make_plan(
        [
            make_step("s1", "test.s1"),
            make_step("s2", "test.s2", depends_on=["s1"]),
            make_step("s3", "test.s3", depends_on=["s2"]),
        ]
    )

    crashed = False
    try:
        _runner(stores, registry).run(plan)
    except SimulatedCrash:
        crashed = True

    stores2 = reopen_stores(stores)  # new connections == new process
    try:
        mode["crash"] = False
        report2 = _runner(stores2, registry).resume(plan)
        events = stores2.events.stream(str(plan.run_id))
        resumed_event = next((e for e in events if e.type == EventType.RUN_RESUMED), None)
        return {
            "crashed": crashed,
            "run_status": report2.status.value,
            "resumed": report2.resume.resumed,
            "restored_steps": report2.resume.restored_steps,
            "s1_calls": len(s1_calls),
            "s2_calls": len(s2_calls),
            "s3_calls": len(s3_calls),
            "config_reported": bool(report2.resume.config_fingerprint)
            and report2.resume.config_fingerprint == report2.resume.stored_config_fingerprint,
            "resume_event_names_checkpoint": resumed_event is not None
            and resumed_event.payload.get("from_checkpoints") == {"s1": 1},
        }
    finally:
        stores2.close()


# ---------------------------------------------------------------------------
# e) same idempotency key submitted twice (two different runs)
# ---------------------------------------------------------------------------

def scenario_duplicate_idempotency_key(work_dir: Path) -> dict[str, Any]:
    stores = Stores(work_dir / "e.db")
    key = "k-dup-submit"
    try:
        registry = build_default_registry()
        runner = _runner(stores, registry)
        outputs = []
        for _ in range(2):
            plan = make_plan(
                [
                    make_step(
                        "export",
                        "report.export",
                        arguments={"title": "T", "content": "body", "idempotency_key": key},
                        risk=Risk.BOUNDED_WRITE,
                        idempotency_key=key,
                    )
                ]
            )
            report = runner.run(plan)
            assert report.status == RunState.SUCCEEDED
            outputs.append(report.results["export"].output)
        return {
            "run_status": "succeeded",
            "effect_count": stores.ledger.effect_count_for(key),
            "replayed": outputs[1].get("idempotent_replay") is True,
            "same_artifact": outputs[0].get("artifact") == outputs[1].get("artifact"),
        }
    finally:
        stores.close()


# ---------------------------------------------------------------------------
# f) illegal state transition -> typed error
# ---------------------------------------------------------------------------

def scenario_illegal_transition(work_dir: Path) -> dict[str, Any]:
    plan = make_plan([make_step("s1", "evidence.search", arguments={"query": "x"})])
    graph = StateGraph.from_plan(plan)
    step_error = run_error = None
    try:
        graph.transition("s1", StepState.SUCCEEDED)  # pending -> succeeded is illegal
    except IllegalTransitionError as exc:
        step_error = exc
    try:
        guard_run_transition(RunState.SUCCEEDED, RunState.RUNNING)
    except IllegalTransitionError as exc:
        run_error = exc
    graph.transition("s1", StepState.RUNNING)  # legal path still works
    graph.transition("s1", StepState.SUCCEEDED)
    return {
        "typed_error_raised": isinstance(step_error, IllegalTransitionError)
        and isinstance(run_error, IllegalTransitionError),
        "error_type": type(step_error).__name__ if step_error else "",
        "legal_path_ok": graph.state("s1") == StepState.SUCCEEDED,
    }


# ---------------------------------------------------------------------------
# g) lease expires, another worker takes over; the old owner is fenced
# ---------------------------------------------------------------------------

def scenario_lease_takeover(work_dir: Path) -> dict[str, Any]:
    clock = FakeClock()
    stores = Stores(work_dir / "g.db", clock)
    s1_calls: list[str] = []
    s2_calls: list[str] = []
    registry = build_default_registry()
    register_test_tool(registry, "test.s1", counting_handler(s1_calls, name="s1"))
    register_test_tool(registry, "test.s2", counting_handler(s2_calls, name="s2"))
    plan = make_plan([make_step("s1", "test.s1"), make_step("s2", "test.s2", depends_on=["s1"])])
    run_id = str(plan.run_id)

    taken_over = {"done": False}

    def expire_and_take_over(rid: str, step_id: str) -> None:
        if step_id == "s2" and not taken_over["done"]:
            taken_over["done"] = True
            clock.advance(11.0)  # worker-A ttl is 10s -> lease now expired
            stores.leases.acquire(rid, "worker-B", 10.0)

    runner_a = _runner(stores, registry, owner="worker-A", lease_ttl_s=10.0, before_node=expire_and_take_over)
    report_a = runner_a.run(plan)

    original_owner_rejected = False
    try:
        stores.leases.validate(run_id, "worker-A", 1)
    except LeaseFencingError:
        original_owner_rejected = True

    runner_b = _runner(stores, registry, owner="worker-B", lease_ttl_s=10.0)
    report_b = runner_b.resume(plan, owner="worker-B")
    return {
        "run_status": report_b.status.value,
        "fenced": report_a.fenced,
        "original_owner_rejected": original_owner_rejected,
        "s1_calls": len(s1_calls),
        "s2_calls": len(s2_calls),
    }


# ---------------------------------------------------------------------------
# h) two concurrent runs (threads) must not share events or checkpoints
# ---------------------------------------------------------------------------

def scenario_concurrent_runs(work_dir: Path) -> dict[str, Any]:
    stores = Stores(work_dir / "h.db")
    registry = build_default_registry()
    calls: list[tuple[str, str]] = []

    def rec(arguments, ctx):
        time.sleep(0.002)  # encourage interleaving
        calls.append((ctx.run_id, arguments.get("tag", "")))
        return counting_handler([], name="rec")(arguments, ctx)

    register_test_tool(registry, "test.rec", rec)
    runner = _runner(stores, registry)

    def build(tag: str) -> Any:
        return make_plan(
            [
                make_step("n1", "test.rec", arguments={"tag": tag}),
                make_step("n2", "test.rec", depends_on=["n1"], arguments={"tag": tag}),
                make_step("n3", "test.rec", depends_on=["n2"], arguments={"tag": tag}),
            ],
            run_id=uuid.uuid4(),
        )

    plan_a, plan_b = build("A"), build("B")
    reports: dict[str, Any] = {}

    def drive(plan, label):
        reports[label] = runner.run(plan)

    threads = [
        threading.Thread(target=drive, args=(plan_a, "A")),
        threading.Thread(target=drive, args=(plan_b, "B")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seq_monotonic = True
    no_cross_events = True
    for plan in (plan_a, plan_b):
        rid = str(plan.run_id)
        stream = stores.events.stream(rid)
        if [e.seq for e in stream] != list(range(1, len(stream) + 1)):
            seq_monotonic = False
        if any(e.run_id != rid for e in stream):
            no_cross_events = False
        checkpoints = stores.checkpoints.load_checkpoints(rid)
        if set(checkpoints) != {"n1", "n2", "n3"} or any(
            cp.run_id != rid for cp in checkpoints.values()
        ):
            no_cross_events = False
    both_succeeded = all(r.status == RunState.SUCCEEDED for r in reports.values())
    a_calls = [c for c in calls if c[0] == str(plan_a.run_id)]
    b_calls = [c for c in calls if c[0] == str(plan_b.run_id)]
    return {
        "run_status": "succeeded" if both_succeeded else "failed",
        "both_succeeded": both_succeeded,
        "seq_monotonic": seq_monotonic,
        "no_cross_events": no_cross_events,
        "isolated_tool_calls": len(a_calls) == 3 and len(b_calls) == 3,
    }


DISPATCH: dict[str, Callable[[Path], dict[str, Any]]] = {
    "transient_retry": scenario_transient_retry,
    "terminal_error": scenario_terminal_error,
    "export_then_disconnect": scenario_export_then_disconnect,
    "crash_resume": scenario_crash_resume,
    "duplicate_idempotency_key": scenario_duplicate_idempotency_key,
    "illegal_transition": scenario_illegal_transition,
    "lease_takeover": scenario_lease_takeover,
    "concurrent_runs": scenario_concurrent_runs,
}


def run_case(case: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Execute one frozen case and evaluate it against its expected block."""
    detail = DISPATCH[case["scenario"]](work_dir)
    mismatches = {
        key: {"expected": want, "actual": detail.get(key)}
        for key, want in case["expected"].items()
        if detail.get(key) != want
    }
    return {
        "case_id": case["case_id"],
        "scenario": case["scenario"],
        "recovered": not mismatches,
        "mismatches": mismatches,
        "detail": detail,
    }
