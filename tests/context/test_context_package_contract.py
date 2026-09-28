"""S4-1/S4-3: ContextPackage production contract and evidence preparation
(content dedup, source quota, neighbor expansion, pin protection).
"""

from __future__ import annotations

import pytest

from app.context.engine import ContextEngine, AssembledContext
from app.context.package import ContextPackage, package_from_assembled
from app.context.selector import EvidenceItem
from app.context.quota import (
    dedup_items,
    apply_source_quota,
    expand_neighbors,
)

EVIDENCE = [
    {"evidence_id": "ev-a", "document_id": "doc1", "page": 1, "modality": "text",
     "content": "alpha " * 40, "source_span": {"chunk_id": "doc1:p1:c1"}},
    {"evidence_id": "ev-b", "document_id": "doc1", "page": 1, "modality": "text",
     "content": "beta " * 40, "source_span": {"chunk_id": "doc1:p1:c2"}},
    {"evidence_id": "ev-c", "document_id": "doc2", "page": 3, "modality": "text",
     "content": "gamma " * 40, "source_span": {"chunk_id": "doc2:p3:c0"}},
]


def _items():
    return [
        EvidenceItem(evidence_id=e["evidence_id"], content=e["content"],
                     page=e["page"], document_id=e["document_id"],
                     chunk_id=e["source_span"]["chunk_id"], modality=e["modality"])
        for e in EVIDENCE
    ]


def test_context_package_contract_from_engine_output() -> None:
    engine = ContextEngine()
    assembled = engine.assemble(
        system="sys", active_turn="q", evidence=_items(),
    )
    pkg = package_from_assembled(assembled, engine.config)
    assert isinstance(pkg, ContextPackage)
    assert pkg.package_fingerprint
    assert pkg.token_counter in ("heuristic", "tiktoken")
    assert set(pkg.budgets) >= {"system", "active_turn", "memory", "evidence", "artifact", "tool_result"}
    assert pkg.usage["evidence"] > 0
    ids = [it.evidence_id for it in pkg.items if it.partition == "evidence"]
    assert set(ids) == {"ev-a", "ev-b", "ev-c"}
    for it in pkg.items:
        assert it.tokens > 0
        assert it.source_kind in ("inline", "artifact_pointer")
    # fingerprint stable for identical input
    pkg2 = package_from_assembled(engine.assemble(system="sys", active_turn="q", evidence=_items()), engine.config)
    assert pkg2.package_fingerprint == pkg.package_fingerprint


def test_dedup_by_chunk_id_and_content() -> None:
    items = _items()
    dup_chunk = EvidenceItem(evidence_id="ev-a-v2", content=items[0].content,
                             page=1, document_id="doc1", chunk_id="doc1:p1:c1")
    dup_content = EvidenceItem(evidence_id="ev-x", content=items[0].content,
                               page=9, document_id="doc9", chunk_id="doc9:p9:c9")
    kept, dropped = dedup_items(items + [dup_chunk, dup_content])
    assert {i.evidence_id for i in kept} == {"ev-a", "ev-b", "ev-c"}
    reasons = {d["evidence_id"]: d["reason"] for d in dropped}
    assert "duplicate_chunk_id" in reasons["ev-a-v2"]
    assert "duplicate_content" in reasons["ev-x"]


def test_source_quota_limits_single_document_share() -> None:
    # doc1 has 2 large items; with a tight budget and 60% source cap,
    # the second doc1 item must be dropped with a quota reason while the
    # doc2 item survives
    items = _items()
    budget = items[0].tokens() + items[2].tokens() + 10
    kept, dropped = apply_source_quota(items, budget, max_share=0.6)
    kept_ids = {i.evidence_id for i in kept}
    assert "ev-c" in kept_ids
    assert "ev-a" in kept_ids
    assert "ev-b" not in kept_ids
    assert any("source_quota" in d["reason"] for d in dropped if d["evidence_id"] == "ev-b")


def test_neighbor_expansion_admits_adjacent_chunk_with_lineage() -> None:
    index = {
        "doc1:p1:c1": _items()[0],
        "doc1:p1:c2": _items()[1],
        "doc2:p3:c0": _items()[2],
    }
    expanded, notes = expand_neighbors([_items()[0]], index, max_per_item=1)
    ids = {i.evidence_id for i in expanded}
    assert ids == {"ev-a", "ev-b"}
    neighbor = [i for i in expanded if i.evidence_id == "ev-b"][0]
    assert notes and any("ev-b" in n and "neighbor" in n for n in notes)
    assert neighbor.neighbor_of == "ev-a"


def test_visual_to_text_neighbor_expansion() -> None:
    visual = EvidenceItem(evidence_id="ev-vis", content="figure desc", page=1,
                          document_id="doc1", chunk_id=None, modality="visual")
    text_item = _items()[0]  # doc1:p1:c1
    index = {"doc1:p1:c1": text_item}
    expanded, notes = expand_neighbors([visual], index, max_per_item=1)
    ids = {i.evidence_id for i in expanded}
    assert ids == {"ev-vis", "ev-a"}
    assert any("visual_to_text_neighbor" in n for n in notes)
    neighbor = [i for i in expanded if i.evidence_id == "ev-a"][0]
    assert neighbor.neighbor_of == "ev-vis"
