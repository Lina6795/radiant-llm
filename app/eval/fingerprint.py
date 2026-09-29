"""Configuration fingerprint collection for eval runs.

Every eval report carries a fingerprint so any metric can be traced back
to the exact code, model/embedding configuration, environment and data
versions that produced it. Nothing here is optional: a field that cannot
be collected is recorded as ``None`` (and surfaced), never silently
dropped.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .versions import (
    EVALUATOR_VERSION,
    LAYER_EVALUATOR_VERSIONS,
    METRIC_SCHEMA_VERSION,
    RANDOM_SEED,
    RETRIEVAL_PRESET,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
BASELINE_CONFIG = REPO_ROOT / "configs" / "baseline.yaml"

# Env vars that change eval results. Values are recorded verbatim except
# anything that looks like a secret, which is recorded as "<set>".
ENV_WHITELIST = (
    "HF_ENDPOINT",
    "RADIANT_EVIDENCE_DB",
    "RADIANT_VECTOR_STORE",
    "RADIANT_ARTIFACT_DIR",
    "RADIANT_TOKENIZER_BACKEND",
    "RADIANT_EMBEDDING_PROVIDER",
    "RADIANT_LOCAL_EMBEDDING_MODEL",
    "RADIANT_AGENT_MAX_ITERATIONS",
    "OPENAI_BASE_URL",
    "LANGCHAIN_TRACING_V2",
)

# Dependencies whose versions materially affect metrics.
DEPENDENCIES = (
    "pydantic",
    "pytest",
    "sentence-transformers",
    "tiktoken",
    "chromadb",
    "langchain-chroma",
    "rank-bm25",
)


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_commit() -> Dict[str, Any]:
    def git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "commit": commit,
        "dirty": None if status is None else bool(status),
    }


def _env_snapshot() -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for name in ENV_WHITELIST:
        value = os.environ.get(name)
        if value is not None and ("KEY" in name or "TOKEN" in name or "SECRET" in name):
            value = "<set>"
        out[name] = value
    return out


def _dependency_versions() -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for dist in DEPENDENCIES:
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = None
    return out


def _data_versions() -> Dict[str, Dict[str, Any]]:
    """sha256 + case count for every frozen benchmark file present."""
    out: Dict[str, Dict[str, Any]] = {}
    if not BENCHMARKS_DIR.is_dir():
        return out
    for path in sorted(BENCHMARKS_DIR.glob("*.jsonl")):
        n_cases = sum(
            1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        out[path.name] = {"sha256": _sha256_file(path), "n_cases": n_cases}
    return out


def _model_config() -> Dict[str, Any]:
    """Model/embedding section of the frozen baseline config.

    Parsed with PyYAML when available; otherwise the raw file hash is
    recorded so the content is still pinned.
    """
    info: Dict[str, Any] = {"path": str(BASELINE_CONFIG.relative_to(REPO_ROOT))}
    info["sha256"] = _sha256_file(BASELINE_CONFIG)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(BASELINE_CONFIG.read_text(encoding="utf-8"))
        info["models"] = data.get("models")
        info["retrieval_baseline_a0"] = data.get("retrieval_baseline_a0")
    except Exception:
        info["models"] = None
    return info


# ---------------------------------------------------------------------------
# Comparability metadata (S11-A)
# ---------------------------------------------------------------------------

def _dataset_digest(data_versions: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Aggregate digest over the frozen benchmark files (name -> sha256)."""
    if not data_versions:
        return None
    canonical = json.dumps(
        {name: info.get("sha256") for name, info in sorted(data_versions.items())},
        sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _evidence_store_digest() -> Dict[str, Any]:
    """Content digest of the evidence DB: corpus re-ingestions (which mint
    new evidence_ids and shift rankings) must make runs non-comparable."""
    path = os.environ.get("RADIANT_EVIDENCE_DB")
    info: Dict[str, Any] = {"path": path, "content_digest": None,
                            "n_rows": None, "n_current": None}
    if not path or not Path(path).is_file():
        return info
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT evidence_id, content_hash, valid_to FROM evidence "
                "ORDER BY evidence_id").fetchall()
            n_current = conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE valid_to IS NULL").fetchone()[0]
        finally:
            conn.close()
        digest = hashlib.sha256()
        for row in rows:
            digest.update("|".join(str(x) for x in row).encode("utf-8"))
            digest.update(b"\n")
        info.update(content_digest=digest.hexdigest()[:16],
                    n_rows=len(rows), n_current=n_current)
    except (OSError, sqlite3.Error):
        pass
    return info


def _vector_store_digest() -> Dict[str, Any]:
    """Content digest of the dense vector store directory."""
    path = os.environ.get("RADIANT_VECTOR_STORE")
    info: Dict[str, Any] = {"path": path, "content_digest": None, "n_files": None}
    if not path or not Path(path).is_dir():
        return info
    try:
        digest = hashlib.sha256()
        files = sorted(p for p in Path(path).rglob("*") if p.is_file())
        for file in files:
            digest.update(str(file.relative_to(path)).encode("utf-8"))
            size = file.stat().st_size
            digest.update(str(size).encode("utf-8"))
            if file.suffix in (".sqlite3", ".db") and size < 512 * 1024 * 1024:
                digest.update(file.read_bytes())
        info.update(content_digest=digest.hexdigest()[:16], n_files=len(files))
    except OSError:
        pass
    return info


def _package_source_digest(package_dir: Path) -> Optional[str]:
    """sha256 over every *.py in the package: evaluator-code drift that no
    one version-bumped is still caught (fail-closed net)."""
    if not package_dir.is_dir():
        return None
    digest = hashlib.sha256()
    for file in sorted(package_dir.glob("*.py")):
        digest.update(file.name.encode("utf-8"))
        digest.update(file.read_bytes())
    return digest.hexdigest()[:16]


def _retrieval_config() -> Dict[str, Any]:
    """Pinned retrieval pipeline preset + source digest."""
    info: Dict[str, Any] = {"preset": RETRIEVAL_PRESET, "config": None,
                            "source_digest": None, "config_fingerprint": None}
    try:
        from app.retrieval.pipeline import preset_configs

        preset = preset_configs()[RETRIEVAL_PRESET]
        config = dataclasses.asdict(preset)
        info["config"] = config
    except Exception:
        config = None
    info["source_digest"] = _package_source_digest(REPO_ROOT / "app" / "retrieval")
    canonical = json.dumps({"preset": RETRIEVAL_PRESET, "config": config,
                            "source_digest": info["source_digest"]},
                           sort_keys=True, default=str)
    info["config_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return info


def _verification_config() -> Dict[str, Any]:
    """Verifier/claims source digest: claim-splitting or verdict-rule
    changes redefine every verification metric."""
    source_digest = _package_source_digest(REPO_ROOT / "app" / "verification")
    canonical = json.dumps({"source_digest": source_digest}, sort_keys=True)
    return {
        "source_digest": source_digest,
        "config_fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
    }


def collect_fingerprint() -> Dict[str, Any]:
    """Collect the full configuration fingerprint for the current run."""
    data_versions = _data_versions()
    fp: Dict[str, Any] = {
        "git": _git_commit(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "platform": platform.platform(),
        },
        "env": _env_snapshot(),
        "models": _model_config(),
        "data_versions": data_versions,
        "dependencies": _dependency_versions(),
        # S11-A comparability metadata
        "metric_schema_version": METRIC_SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "layer_evaluator_versions": dict(LAYER_EVALUATOR_VERSIONS),
        "dataset_digest": _dataset_digest(data_versions),
        "random_seed": RANDOM_SEED,
        "evidence_store": _evidence_store_digest(),
        "vector_store": _vector_store_digest(),
        "retrieval": _retrieval_config(),
        "verification": _verification_config(),
    }
    canonical = json.dumps(fp, sort_keys=True, ensure_ascii=False, default=str)
    fp["fingerprint_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return fp
