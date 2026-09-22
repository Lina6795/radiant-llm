"""Configuration fingerprint collection for eval runs.

Every eval report carries a fingerprint so any metric can be traced back
to the exact code, model/embedding configuration, environment and data
versions that produced it. Nothing here is optional: a field that cannot
be collected is recorded as ``None`` (and surfaced), never silently
dropped.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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


def collect_fingerprint() -> Dict[str, Any]:
    """Collect the full configuration fingerprint for the current run."""
    fp: Dict[str, Any] = {
        "git": _git_commit(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "platform": platform.platform(),
        },
        "env": _env_snapshot(),
        "models": _model_config(),
        "data_versions": _data_versions(),
        "dependencies": _dependency_versions(),
    }
    canonical = json.dumps(fp, sort_keys=True, ensure_ascii=False, default=str)
    fp["fingerprint_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return fp
