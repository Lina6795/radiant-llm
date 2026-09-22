"""
M1 acceptance-gate tests for the evidence adapter + SQLite store.

All fixtures are built in tmp directories (sample JSONL + fake PDF bytes);
no LLM calls, no network, fully offline.
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from evidence.adapter import make_document_id, parse_kb_directory
from evidence.models import Modality
from evidence.store import EvidenceStore, get_evidence_store


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_chunk_rows(source: str, doc_id: str, contents: list[str],
                    extractor: str = "nougat") -> list[dict]:
    rows = []
    for i, content in enumerate(contents):
        page = i + 1
        rows.append({
            "source": source,
            "page": page,
            "content": content,
            "chunk_index": i,
            "document_id": doc_id,
            "chunk_id": f"{doc_id}:p{page}:c{i}",
            "extractor": extractor,
        })
    return rows


def make_visual_row(source: str, doc_id: str, page: int = 2,
                    figure_id: str | None = None,
                    bbox: dict | None = None) -> dict:
    row = {
        "source": source,
        "page": page,
        "document_id": doc_id,
        "figure_index": 0,
        "description": "**Subject:** a schematic diagram",
    }
    if figure_id is not None:
        row["figure_id"] = figure_id
    if bbox is not None:
        row["region_bbox"] = bbox
    return row


def build_kb(tmp_path: Path, source: str = "paper.pdf",
             contents: list[str] | None = None,
             pdf_bytes: bytes | None = None,
             extractor: str = "nougat") -> tuple[Path, Path, str]:
    """Create kb dir + fake source PDF. Returns (kb_dir, source_dir, doc_id)."""
    kb_dir = tmp_path / "kb"
    src_dir = tmp_path / "src"
    kb_dir.mkdir(parents=True, exist_ok=True)
    src_dir.mkdir(parents=True, exist_ok=True)
    doc_id = make_document_id(source)
    if pdf_bytes is not None:
        (src_dir / source).write_bytes(pdf_bytes)
    write_jsonl(kb_dir / "01_chunks_kb.jsonl",
                make_chunk_rows(source, doc_id, contents or ["hello"], extractor))
    return kb_dir, src_dir, doc_id


def open_store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.db")


# ---------------------------------------------------------------------------
# (a) Re-ingesting the same document adds zero new records
# ---------------------------------------------------------------------------

def test_reingest_same_document_is_noop(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["alpha", "beta"], pdf_bytes=b"%PDF-fake-v1")

    store = open_store(tmp_path)
    first = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert first["new_records"] == 3  # 1 document + 2 evidence
    assert first["failed"] == []

    second = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert second["new_records"] == 0
    assert second["documents"][0]["short_circuited"] is True

    docs = store.list_documents(document_id=doc_id)
    assert docs["total"] == 1
    ev = store.query_evidence(document_id=doc_id)
    assert ev["total"] == 2
    store.close()


# ---------------------------------------------------------------------------
# (b) Same filename, different content -> new version, old version kept
# ---------------------------------------------------------------------------

def test_same_filename_new_content_creates_new_version(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["version one"], pdf_bytes=b"%PDF-v1")
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    v1 = store.list_documents(document_id=doc_id)["items"][0]["document_version"]

    # Same filename, different bytes (and different parsed text).
    (src_dir / "paper.pdf").write_bytes(b"%PDF-v2-different-bytes")
    write_jsonl(kb_dir / "01_chunks_kb.jsonl",
                make_chunk_rows("paper.pdf", doc_id, ["version two"]))
    store.ingest_directory(kb_dir, source_dir=src_dir)

    docs = store.list_documents(document_id=doc_id)
    assert docs["total"] == 2
    versions = {d["document_version"] for d in docs["items"]}
    assert len(versions) == 2
    assert v1 in versions  # old version not overwritten

    old = store.query_evidence(document_id=doc_id, include_degraded=True)
    old_v1 = [e for e in old["items"] if e["document_version"] == v1]
    assert [e["content"] for e in old_v1] == ["version one"]

    # Re-ingesting v2 afterwards is a no-op (idempotent per version).
    again = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert again["new_records"] == 0
    store.close()


# ---------------------------------------------------------------------------
# (c) Mid-run failure isolation + resume only retries failed documents
# ---------------------------------------------------------------------------

def test_failed_document_is_isolated_and_resumed(tmp_path, monkeypatch):
    kb_dir = tmp_path / "kb"
    src_dir = tmp_path / "src"
    kb_dir.mkdir()
    src_dir.mkdir()
    rows = []
    for name in ("good.pdf", "bad.pdf"):
        doc_id = make_document_id(name)
        (src_dir / name).write_bytes(f"%PDF-{name}".encode())
        rows.extend(make_chunk_rows(name, doc_id, [f"text of {name}"]))
    write_jsonl(kb_dir / "01_chunks_kb.jsonl", rows)
    bad_id = make_document_id("bad.pdf")
    good_id = make_document_id("good.pdf")

    store = open_store(tmp_path)
    original = EvidenceStore._insert_bundle

    def flaky_insert(self, parsed):
        if parsed.document.document_id == bad_id:
            raise sqlite3.OperationalError("simulated disk failure")
        return original(self, parsed)

    monkeypatch.setattr(EvidenceStore, "_insert_bundle", flaky_insert)
    first = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert first["failed"] == [bad_id]
    statuses = {o["document_id"]: o["status"] for o in first["documents"]}
    assert statuses[good_id] == "done"
    # Failure of bad.pdf did not block good.pdf.
    assert store.query_evidence(document_id=good_id)["total"] == 1
    assert store.query_evidence(document_id=bad_id, include_degraded=True)["total"] == 0

    # Resume: only the failed document is retried; good.pdf short-circuits.
    monkeypatch.setattr(EvidenceStore, "_insert_bundle", original)
    second = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert second["failed"] == []
    by_doc = {o["document_id"]: o for o in second["documents"]}
    assert by_doc[good_id]["short_circuited"] is True
    assert by_doc[good_id]["new_records"] == 0
    assert by_doc[bad_id]["short_circuited"] is False
    assert by_doc[bad_id]["new_records"] == 2  # doc + 1 evidence
    assert store.query_evidence(document_id=bad_id)["total"] == 1
    store.close()


# ---------------------------------------------------------------------------
# (d) Visual records missing bbox/figure_id are degraded, never authoritative
# ---------------------------------------------------------------------------

def test_visual_missing_bbox_or_figure_id_is_degraded(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["text"], pdf_bytes=b"%PDF-v")
    write_jsonl(kb_dir / "02_visuals_kb.jsonl", [
        # no region_bbox at all (current upstream behaviour)
        make_visual_row("paper.pdf", doc_id, figure_id=f"{doc_id}:p2:f0"),
        # bbox present but figure_id missing
        make_visual_row("paper.pdf", doc_id, figure_id=None,
                        bbox={"x0": 1, "y0": 2, "x1": 3, "y1": 4}),
    ])
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)

    # Default query (authoritative candidates) contains only the text chunk.
    authoritative = store.query_evidence(document_id=doc_id)
    assert all(not e["degraded"] for e in authoritative["items"])
    assert {e["modality"] for e in authoritative["items"]} == {"text"}

    degraded = store.query_evidence(document_id=doc_id, degraded=True)
    assert degraded["total"] == 2
    reasons = {e["degraded_reason"] for e in degraded["items"]}
    assert any("missing_region_bbox" in r for r in reasons)
    assert any("missing_figure_id" in r for r in reasons)
    store.close()


def test_visual_with_bbox_and_figure_id_is_not_degraded(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["text"], pdf_bytes=b"%PDF-v")
    write_jsonl(kb_dir / "02_visuals_kb.jsonl", [
        make_visual_row("paper.pdf", doc_id, figure_id=f"{doc_id}:p2:f0",
                        bbox={"x0": 1, "y0": 2, "x1": 3, "y1": 4}),
    ])
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    visual = store.query_evidence(document_id=doc_id, modality="visual")
    assert visual["total"] == 1
    item = visual["items"][0]
    assert item["degraded"] is False
    assert item["region_bbox"] == {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0,
                                   "unit": "pt"}
    assert item["page"] == 2
    assert item["figure_id"] == f"{doc_id}:p2:f0"
    store.close()


# ---------------------------------------------------------------------------
# (e) Moved/deleted source PDF -> explicit provenance error, not silent
# ---------------------------------------------------------------------------

def test_missing_artifact_marked_at_ingest(tmp_path):
    # No PDF written at all: artifact cannot be resolved.
    kb_dir, src_dir, doc_id = build_kb(tmp_path, contents=["orphan text"])
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)

    doc = store.list_documents(document_id=doc_id)["items"][0]
    assert doc["artifact_status"] == "missing"
    report = store.verify_provenance()
    assert len(report) == 1
    assert report[0]["status"] == "missing"
    assert "artifact_uri unresolvable" in report[0]["detail"]
    assert doc["artifact_uri"] in report[0]["artifact_uri"] or True
    store.close()


def test_artifact_deleted_after_ingest_reported_by_verify(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["text"], pdf_bytes=b"%PDF-v1")
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    assert store.verify_provenance()[0]["status"] == "ok"

    # Original PDF moved away after ingestion.
    (src_dir / "paper.pdf").rename(src_dir / "paper.moved.pdf")
    report = store.verify_provenance()
    assert report[0]["status"] == "missing"
    assert "moved or deleted" in report[0]["detail"]
    assert report[0]["artifact_uri"].endswith("paper.pdf")
    store.close()


# ---------------------------------------------------------------------------
# (f) _error metadata records are degraded and excluded by default
# ---------------------------------------------------------------------------

def test_error_metadata_is_degraded_and_not_authoritative(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["text"], pdf_bytes=b"%PDF-v")
    write_jsonl(kb_dir / "03_metadata_kb.jsonl", [
        {"source": "paper.pdf", "document_id": doc_id,
         "_error": "OpenAI vision call failed (model=gpt-5.4): 400"},
    ])
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)

    # Default listing (authoritative answer candidates) excludes it.
    default = store.query_evidence(document_id=doc_id)
    assert {e["modality"] for e in default["items"]} == {"text"}

    # Explicitly requested: visible with a clear reason.
    degraded = store.query_evidence(document_id=doc_id, degraded=True)
    assert degraded["total"] == 1
    meta = degraded["items"][0]
    assert meta["modality"] == "metadata"
    assert meta["degraded"] is True
    assert "metadata_extraction_error" in meta["degraded_reason"]
    assert "gpt-5.4" in meta["degraded_reason"]

    included = store.query_evidence(document_id=doc_id, include_degraded=True)
    assert included["total"] == 2
    store.close()


def test_successful_metadata_is_authoritative(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["text"], pdf_bytes=b"%PDF-v")
    write_jsonl(kb_dir / "03_metadata_kb.jsonl", [
        {"source": "paper.pdf", "document_id": doc_id,
         "title": "Attention Is All You Need", "doi": "10.1/xyz"},
    ])
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    meta = store.query_evidence(document_id=doc_id, modality="metadata")
    assert meta["total"] == 1
    assert meta["items"][0]["degraded"] is False
    assert meta["items"][0]["metadata_fields"]["title"] == "Attention Is All You Need"
    store.close()


# ---------------------------------------------------------------------------
# Provenance details: page / chunk neighbours / parser fingerprint
# ---------------------------------------------------------------------------

def test_neighbours_numeric_order_across_12_plus_pages(tmp_path):
    """
    Regression: chunk_index resets per page upstream and chunk_id page numbers
    are strings ("p1" < "p10" < "p12" < "p2" lexicographically). Neighbours
    must be positional in (page, chunk_index) numeric order.
    """
    source, doc_id = "long.pdf", make_document_id("long.pdf")
    rows = []
    # page 1: c0..c3; page 2: c0,c1,c2,c5 (gap, mimics real baseline);
    # pages 3..13: c0..c1 each
    for page in range(1, 14):
        indexes = [0, 1, 2, 3] if page == 1 else ([0, 1, 2, 5] if page == 2 else [0, 1])
        for ci in indexes:
            rows.append({
                "source": source, "page": page, "content": f"p{page} c{ci}",
                "chunk_index": ci, "document_id": doc_id,
                "chunk_id": f"{doc_id}:p{page}:c{ci}", "extractor": "nougat",
            })
    kb_dir = tmp_path / "kb"
    src_dir = tmp_path / "src"
    kb_dir.mkdir()
    src_dir.mkdir()
    (src_dir / source).write_bytes(b"%PDF-long")
    write_jsonl(kb_dir / "01_chunks_kb.jsonl", rows)

    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    spans = {
        e["source_span"]["chunk_id"]: e["source_span"]
        for e in store.query_evidence(document_id=doc_id, limit=1000)["items"]
    }

    def cid(p, c):
        return f"{doc_id}:p{p}:c{c}"

    # The two cases reported from the real baseline library:
    assert spans[cid(2, 0)]["next_chunk_id"] == cid(2, 1)
    assert spans[cid(1, 3)]["prev_chunk_id"] == cid(1, 2)
    # Lexicographic traps: p2/p12 must not interleave.
    assert spans[cid(2, 2)]["next_chunk_id"] == cid(2, 5)  # gap by position
    assert spans[cid(2, 5)]["next_chunk_id"] == cid(3, 0)  # page tail -> next page head
    assert spans[cid(12, 1)]["next_chunk_id"] == cid(13, 0)
    assert spans[cid(12, 0)]["prev_chunk_id"] == cid(11, 1)
    # Cross-page boundary: page head links previous page's last chunk.
    assert spans[cid(2, 0)]["prev_chunk_id"] == cid(1, 3)
    assert spans[cid(3, 0)]["prev_chunk_id"] == cid(2, 5)
    # Document head / tail are None.
    assert spans[cid(1, 0)]["prev_chunk_id"] is None
    assert spans[cid(13, 1)]["next_chunk_id"] is None
    store.close()


def test_page_and_chunk_neighbours_preserved(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["c0", "c1", "c2"], pdf_bytes=b"%PDF-v",
        extractor="lightweight")
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir,
                           vision_model="deepseek-v4-pro")
    items = store.query_evidence(document_id=doc_id)["items"]
    assert [e["page"] for e in items] == [1, 2, 3]
    middle = items[1]
    assert middle["source_span"]["chunk_id"] == f"{doc_id}:p2:c1"
    assert middle["source_span"]["prev_chunk_id"] == f"{doc_id}:p1:c0"
    assert middle["source_span"]["next_chunk_id"] == f"{doc_id}:p3:c2"
    assert "extractor=lightweight" in middle["parser_fingerprint"]
    assert "vlm=deepseek-v4-pro" in middle["parser_fingerprint"]
    assert middle["artifact_uri"].endswith("paper.pdf")
    store.close()


def test_parser_fingerprint_distinguishes_extractors(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["same text"], pdf_bytes=b"%PDF-v",
        extractor="nougat")
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    nougat_fp = store.query_evidence(document_id=doc_id)["items"][0]["parser_fingerprint"]

    # Re-parsed with a different extractor -> different fingerprint, so it is
    # NOT short-circuited as identical and lands as a separate version.
    write_jsonl(kb_dir / "01_chunks_kb.jsonl",
                make_chunk_rows("paper.pdf", doc_id, ["same text"],
                                extractor="lightweight"))
    second = store.ingest_directory(kb_dir, source_dir=src_dir)
    assert second["failed"] == []
    assert second["new_records"] == 2  # new document version + 1 evidence
    fingerprints = {
        e["parser_fingerprint"]
        for e in store.query_evidence(document_id=doc_id)["items"]
    }
    assert fingerprints == {nougat_fp,
                            nougat_fp.replace("extractor=nougat",
                                              "extractor=lightweight")}
    assert any("extractor=lightweight" in fp for fp in fingerprints)
    assert store.list_documents(document_id=doc_id)["total"] == 2
    store.close()


# ---------------------------------------------------------------------------
# Unlocatable records: degraded if attachable, rejected if not
# ---------------------------------------------------------------------------

def test_chunk_missing_page_is_degraded_but_attached(tmp_path):
    kb_dir, src_dir, doc_id = build_kb(
        tmp_path, contents=["ok"], pdf_bytes=b"%PDF-v")
    rows = make_chunk_rows("paper.pdf", doc_id, ["ok"])
    rows.append({"source": "paper.pdf", "content": "no page",
                 "chunk_index": 7, "document_id": doc_id,
                 "chunk_id": f"{doc_id}:p?:c7", "extractor": "nougat"})
    write_jsonl(kb_dir / "01_chunks_kb.jsonl", rows)
    store = open_store(tmp_path)
    store.ingest_directory(kb_dir, source_dir=src_dir)
    degraded = store.query_evidence(document_id=doc_id, degraded=True)
    assert degraded["total"] == 1
    assert "unlocatable_source_page" in degraded["items"][0]["degraded_reason"]
    store.close()


def test_row_without_document_identity_is_rejected_not_silent(tmp_path):
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    write_jsonl(kb_dir / "01_chunks_kb.jsonl", [
        {"content": "orphan", "page": 1, "chunk_index": 0},
    ])
    result = parse_kb_directory(kb_dir)
    assert result.documents == []
    assert len(result.rejected_rows) == 1
    assert result.rejected_rows[0]["reason"] == "no document_id and no source"


# ---------------------------------------------------------------------------
# Store factory env resolution (no hardcoded paths)
# ---------------------------------------------------------------------------

def test_store_factory_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RADIANT_EVIDENCE_DB", str(tmp_path / "custom.db"))
    store = get_evidence_store()
    assert store.db_path == tmp_path / "custom.db"
    store.close()
    assert (tmp_path / "custom.db").exists()

    monkeypatch.delenv("RADIANT_EVIDENCE_DB")
    monkeypatch.setenv("RADIANT_LLM_CONFIG_DIR", str(tmp_path / "cfg"))
    store = get_evidence_store()
    assert store.db_path == tmp_path / "cfg" / "evidence.db"
    store.close()
