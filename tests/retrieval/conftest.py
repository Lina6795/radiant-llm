import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))

# Real-baseline locations for the smoke test. Overridable via env; the smoke
# test skips (never fails) when they are absent.
BASELINE_DIR = Path(
    os.getenv("RADIANT_BASELINE_DIR",
              "/mnt/lina/radiant-llm/artifacts/baseline/m0-20260922"))
# NOTE: RADIANT_EVIDENCE_DB is the *runtime store* env var (tests/api sets it
# to a session-tmp DB); reusing it here silently retargets this smoke test at
# the wrong database (masked before by the skip below). Use a baseline-specific
# override instead.
BASELINE_EVIDENCE_DB = Path(
    os.getenv("RADIANT_BASELINE_EVIDENCE_DB", str(BASELINE_DIR / "evidence.db")))
BASELINE_VECTOR_STORE = Path(
    os.getenv("RADIANT_VECTOR_STORE",
              str(BASELINE_DIR / "output" / "local_vector_store")))


@pytest.fixture
def sample_evidence():
    """Synthetic authoritative + degraded text evidence payloads."""
    rows = []

    def add(eid, content, page, doc="docA", degraded=False,
            authority="primary", workspace="default", version="v1",
            chunk=None):
        rows.append({
            "evidence_id": eid,
            "workspace_id": workspace,
            "document_id": doc,
            "document_version": version,
            "modality": "text",
            "page": page,
            "degraded": degraded,
            "degraded_reason": "ocr_failed" if degraded else None,
            "authority_level": authority,
            "content": content,
            "source_span": {"chunk_id": chunk or f"{doc}:p{page}:c{eid[-1]}"},
            "artifact_uri": f"file:///kb/{doc}.pdf",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": None,
        })

    add("ev-a1", "the transformer uses self attention mechanisms", 1)
    add("ev-a2", "attention is all you need for sequence transduction", 1)
    add("ev-a3", "the encoder and decoder stacks contain six layers", 2)
    add("ev-a4", "dropout and label smoothing are regularization tools", 3)
    add("ev-a5", "the quick brown fox jumps over the lazy dog", 4)
    add("ev-d1", "garbled degraded attention fragment", 5, degraded=True)
    add("ev-b1", "secondary commentary on attention from another doc", 6,
        doc="docB", authority="secondary", workspace="other")
    return rows
