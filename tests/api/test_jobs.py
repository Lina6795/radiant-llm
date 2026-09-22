"""M9 background jobs: benchmark runs (M8 eval runner) and document ingest."""

from __future__ import annotations

import shutil

from conftest import wait_for, write_jsonl


def test_benchmark_run_state_machine(client):
    resp = client.post("/benchmarks/run", json={"layers": ["control"]})
    assert resp.status_code == 202, resp.text
    benchmark_id = resp.json()["benchmark_id"]
    out_dir = resp.json()["out_dir"]
    try:
        job = wait_for(
            lambda: (lambda j: j if j["status"] != "running" else None)(client.get(f"/benchmarks/{benchmark_id}").json()),
            timeout=120.0,
            interval=1.0,
            desc="benchmark completion",
        )
        assert job["status"] == "done", job
        assert job["exit_code"] == 0
        report = job["report"]
        assert report is not None
        summary = report["summary"]
        assert summary["cases_total"] > 0
        assert summary["cases_failed"] == 0
        assert report["failed_cases"] == []
        # comparison vs the default M8 baseline is attached
        assert report["baseline_comparison"] is not None
        assert report["baseline_comparison"]["baseline_run_id"] == "m8-baseline-20260922"
        assert report["metrics_flat"]

        listing = client.get("/benchmarks").json()
        assert any(item["benchmark_id"] == benchmark_id for item in listing["items"])
        # the M8 baseline itself is listed as a completed benchmark
        assert any(item["benchmark_id"] == "m8-baseline-20260922" for item in listing["items"])
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_benchmark_validation_and_404(client):
    resp = client.post("/benchmarks/run", json={"layers": ["no-such-layer"]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_layers"

    resp = client.post("/benchmarks/run", json={"baseline": "/nonexistent/baseline"})
    assert resp.status_code == 400

    resp = client.get("/benchmarks/bm-does-not-exist")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def _write_minimal_kb(kb_dir, source: str = "paper-x.pdf", doc_id: str = "doc-m9-test"):
    rows = [
        {
            "source": source,
            "page": 1,
            "content": "Passive decay heat removal keeps the core safe without AC power.",
            "chunk_index": 0,
            "document_id": doc_id,
            "chunk_id": f"{doc_id}:p1:c0",
            "extractor": "lightweight",
        },
        {
            "source": source,
            "page": 2,
            "content": "The reactor protection system inserts control rods on trip signals.",
            "chunk_index": 1,
            "document_id": doc_id,
            "chunk_id": f"{doc_id}:p2:c1",
            "extractor": "lightweight",
        },
    ]
    write_jsonl(kb_dir / "01_chunks_kb.jsonl", rows)


def test_ingest_job_lifecycle(client, tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    _write_minimal_kb(kb_dir)

    resp = client.post("/documents/ingest", json={"input_dir": str(kb_dir), "workspace_id": "m9-ingest-test"})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = wait_for(
        lambda: (lambda j: j if j["status"] in {"done", "failed"} else None)(client.get(f"/documents/ingest/{job_id}").json()),
        timeout=30.0,
        desc="ingest job completion",
    )
    assert job["status"] == "done", job
    assert job["result"]["new_records"] >= 1
    assert job["result"]["failed"] == []

    # idempotent re-ingest short-circuits
    resp = client.post("/documents/ingest", json={"input_dir": str(kb_dir), "workspace_id": "m9-ingest-test"})
    job_id2 = resp.json()["job_id"]
    job2 = wait_for(
        lambda: (lambda j: j if j["status"] in {"done", "failed"} else None)(client.get(f"/documents/ingest/{job_id2}").json()),
        timeout=30.0,
        desc="re-ingest completion",
    )
    assert job2["status"] == "done"
    assert job2["result"]["new_records"] == 0

    # ingested evidence is queryable through the M1 endpoint
    ev = client.get("/evidence", params={"workspace_id": "m9-ingest-test"}).json()
    assert ev["total"] >= 2


def test_ingest_errors(client, tmp_path):
    resp = client.post("/documents/ingest", json={})
    assert resp.status_code == 400
    resp = client.post("/documents/ingest", json={"input_dir": str(tmp_path / "missing")})
    assert resp.status_code == 400

    # empty directory with parse disabled -> job fails with an explicit reason
    empty = tmp_path / "empty"
    empty.mkdir()
    resp = client.post("/documents/ingest", json={"input_dir": str(empty), "parse": False})
    job_id = resp.json()["job_id"]
    job = wait_for(
        lambda: (lambda j: j if j["status"] in {"done", "failed"} else None)(client.get(f"/documents/ingest/{job_id}").json()),
        timeout=15.0,
        desc="empty ingest failure",
    )
    assert job["status"] == "failed"
    assert "no KB JSONL" in job["error"]

    resp = client.get("/documents/ingest/ing-missing")
    assert resp.status_code == 404
