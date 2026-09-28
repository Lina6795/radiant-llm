"""S10: post-answer memory candidate generation (pure unit tests, no API).

The Write Gate stays default-deny; candidates are only ever proposals.
API-level hook tests live in tests/api/test_memories.py (importing ``api``
requires the env fixtures provided by tests/api/conftest.py).
"""

from __future__ import annotations

from app.memory.candidates import candidates_from_run
from app.memory.models import MemoryCategory

CLAIMS = [
    {"verdict": "supported", "evidence_ids": ["ev-1", "ev-2"], "page": 3,
     "text": "The Transformer has an encoder and a decoder."},
    {"verdict": "unsupported", "evidence_ids": ["ev-9"], "page": 5,
     "text": "It was invented in 2016."},
]


def test_candidates_only_session_and_evidence_pointer():
    cands = candidates_from_run(
        run_id="run-1", goal="q", answer="An encoder and a decoder.",
        claims=CLAIMS, workspace="default")
    categories = {c.category for c in cands}
    assert categories <= {MemoryCategory.SESSION, MemoryCategory.EVIDENCE_POINTER}
    assert MemoryCategory.USER_FACT not in categories
    assert MemoryCategory.DECISION not in categories

    session = [c for c in cands if c.category == MemoryCategory.SESSION]
    assert len(session) == 1
    assert session[0].provenance.source_run_id == "run-1"
    assert session[0].ttl_seconds is not None
    assert session[0].confidence == 0.5  # 1/2 supported

    pointers = [c for c in cands if c.category == MemoryCategory.EVIDENCE_POINTER]
    # only supported claims contribute; ev-9 (unsupported) excluded; dedup applied
    assert {p.provenance.evidence_id for p in pointers} == {"ev-1", "ev-2"}
    assert all(p.write_reason.startswith("post_answer:auto") for p in cands)


def test_candidates_empty_answer_no_session():
    cands = candidates_from_run(run_id="run-2", goal="q", answer="", claims=[],
                                workspace="default")
    assert cands == []


def test_candidates_no_claims_neutral_confidence():
    cands = candidates_from_run(run_id="run-3", goal="q", answer="ok", claims=[],
                                workspace="default")
    assert len(cands) == 1
    assert cands[0].confidence == 0.5
