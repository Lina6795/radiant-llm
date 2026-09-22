"""Registry discovery and fingerprint collection."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.eval.fingerprint import collect_fingerprint
from app.eval.registry import BENCHMARKS_DIR, DATASETS, discover, specs_for_layer


class TestRegistry:
    def test_registered_datasets_discovered(self):
        discovery = discover()
        present = {name for name, d in discovery["datasets"].items()
                   if d.status == "present"}
        # The seven M0-M6 frozen sets must always be there.
        assert {"router_cases", "policy_cases", "retrieval_cases",
                "baseline_cases", "context_cases", "memory_cases",
                "runtime_cases"} <= present

    def test_case_counts_match_files(self):
        discovery = discover()
        for name, d in discovery["datasets"].items():
            if d.status != "present":
                continue
            n = sum(1 for line in d.spec.path.read_text(encoding="utf-8").splitlines()
                    if line.strip())
            assert d.n_cases == n

    def test_unregistered_files_detected(self, tmp_path):
        (tmp_path / "router_cases.jsonl").write_text('{"case_id": "X"}\n',
                                                     encoding="utf-8")
        (tmp_path / "rogue_cases.jsonl").write_text('{"case_id": "Y"}\n',
                                                    encoding="utf-8")
        discovery = discover(benchmarks_dir=tmp_path)
        assert discovery["unregistered_files"] == ["rogue_cases.jsonl"]
        assert discovery["datasets"]["router_cases"].status == "present"
        assert discovery["datasets"]["memory_cases"].status == "missing"
        assert "not found" in discovery["datasets"]["memory_cases"].reason

    def test_specs_unique_and_layered(self):
        names = [s.name for s in DATASETS]
        assert len(names) == len(set(names))
        assert {s.layer for s in DATASETS} == {
            "control", "durable", "retrieval", "context", "memory", "verification"}
        assert {s.name for s in specs_for_layer("control")} == {
            "router_cases", "policy_cases"}


class TestFingerprint:
    def test_required_sections(self):
        fp = collect_fingerprint()
        for key in ("git", "python", "env", "models", "data_versions",
                    "dependencies", "fingerprint_hash"):
            assert key in fp, key
        assert fp["git"]["commit"]
        assert fp["fingerprint_hash"]

    def test_data_version_hashes_match_files(self):
        fp = collect_fingerprint()
        path = BENCHMARKS_DIR / "router_cases.jsonl"
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert fp["data_versions"]["router_cases.jsonl"]["sha256"] == expected
        assert fp["data_versions"]["router_cases.jsonl"]["n_cases"] == 12

    def test_fingerprint_hash_stable_within_process(self):
        assert collect_fingerprint()["fingerprint_hash"] == \
            collect_fingerprint()["fingerprint_hash"]

    def test_secret_env_values_masked(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com")
        fp = collect_fingerprint()
        assert fp["env"]["HF_ENDPOINT"] == "https://hf-mirror.com"
