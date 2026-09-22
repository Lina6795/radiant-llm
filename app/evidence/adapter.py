"""
Adapter: convert the upstream Visual-Parser three-file JSONL output
(01_chunks_kb.jsonl / 02_visuals_kb.jsonl / 03_metadata_kb.jsonl) into
Document + Evidence records.

Degradation rules — never silently promote incomplete records to
authoritative evidence:
- visual record without region_bbox or figure_id  -> degraded
- metadata record carrying ``_error``             -> degraded
- any record that cannot be located to source+page -> degraded
- rows that cannot be attached to any document at all are returned in
  ``AdapterResult.rejected_rows`` (surfaced, not dropped silently).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    Document,
    Evidence,
    Figure,
    Modality,
    Page,
    RegionBBox,
    SourceSpan,
    hash_bytes,
    make_document_version,
    make_evidence_id,
    make_parser_fingerprint,
)

CHUNK_ROWS_FILENAME = "01_chunks_kb.jsonl"
VISUAL_ROWS_FILENAME = "02_visuals_kb.jsonl"
METADATA_ROWS_FILENAME = "03_metadata_kb.jsonl"


def make_document_id(source: str) -> str:
    # Must stay byte-identical to utils.vp_jsonl_writer.make_document_id so
    # adapter-derived IDs match upstream rows that omit document_id.
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _row_document_id(row: Dict[str, Any]) -> Optional[str]:
    doc_id = row.get("document_id")
    if isinstance(doc_id, str) and doc_id:
        return doc_id
    source = row.get("source")
    if isinstance(source, str) and source:
        return make_document_id(source)
    return None


def _resolve_artifact(
    source: Optional[str],
    kb_dir: Path,
    source_dir: Optional[Path],
) -> tuple[str, str, Optional[Path]]:
    """
    Return (artifact_uri, artifact_status, resolved_path).

    The original file may live in ``source_dir`` (parser input dir) or next
    to the JSONL files. When it cannot be resolved the URI is still recorded
    (pointing at the expected location) and the status is ``missing`` so the
    provenance break is explicit rather than silent.
    """
    if not source:
        return ("file://unknown", "missing", None)
    candidates: List[Path] = []
    if source_dir is not None:
        candidates.append(Path(source_dir) / source)
    candidates.append(Path(kb_dir) / source)
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            return (f"file://{resolved}", "ok", resolved)
    expected = candidates[0].resolve()
    return (f"file://{expected}", "missing", None)


def _content_hash(
    resolved_path: Optional[Path],
    records: List[Dict[str, Any]],
) -> str:
    if resolved_path is not None:
        return hash_bytes(resolved_path.read_bytes())
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True)
    return hash_bytes(canonical.encode("utf-8"))


def _parse_bbox(raw: Any) -> Optional[RegionBBox]:
    if not isinstance(raw, dict):
        return None
    try:
        return RegionBBox(
            x0=float(raw["x0"]),
            y0=float(raw["y0"]),
            x1=float(raw["x1"]),
            y1=float(raw["y1"]),
            unit=str(raw.get("unit", "pt")),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class ParsedDocument:
    document: Document
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class AdapterResult:
    documents: List[ParsedDocument] = field(default_factory=list)
    rejected_rows: List[Dict[str, Any]] = field(default_factory=list)


def parse_kb_directory(
    kb_dir: str | os.PathLike[str],
    workspace_id: str = "default",
    source_dir: Optional[str | os.PathLike[str]] = None,
    vision_model: Optional[str] = None,
) -> AdapterResult:
    """
    Parse the three KB JSONL files in ``kb_dir`` into documents and evidence.

    ``vision_model`` identifies the VLM that produced 02/03 records; it is
    folded into the parser fingerprint. It is never called — the adapter is
    fully offline.
    """
    kb_path = Path(kb_dir)
    src_path = Path(source_dir) if source_dir is not None else None

    chunk_rows = _read_jsonl(kb_path / CHUNK_ROWS_FILENAME)
    visual_rows = _read_jsonl(kb_path / VISUAL_ROWS_FILENAME)
    metadata_rows = _read_jsonl(kb_path / METADATA_ROWS_FILENAME)

    chunks_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    visuals_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    metadata_by_doc: Dict[str, List[Dict[str, Any]]] = {}
    rejected: List[Dict[str, Any]] = []

    for row in chunk_rows:
        doc_id = _row_document_id(row)
        if doc_id is None:
            rejected.append({"file": CHUNK_ROWS_FILENAME, "row": row,
                             "reason": "no document_id and no source"})
            continue
        chunks_by_doc.setdefault(doc_id, []).append(row)
    for row in visual_rows:
        doc_id = _row_document_id(row)
        if doc_id is None:
            rejected.append({"file": VISUAL_ROWS_FILENAME, "row": row,
                             "reason": "no document_id and no source"})
            continue
        visuals_by_doc.setdefault(doc_id, []).append(row)
    for row in metadata_rows:
        doc_id = _row_document_id(row)
        if doc_id is None:
            rejected.append({"file": METADATA_ROWS_FILENAME, "row": row,
                             "reason": "no document_id and no source"})
            continue
        metadata_by_doc.setdefault(doc_id, []).append(row)

    result = AdapterResult(rejected_rows=rejected)
    all_doc_ids = sorted(
        set(chunks_by_doc) | set(visuals_by_doc) | set(metadata_by_doc)
    )

    for doc_id in all_doc_ids:
        chunks = sorted(
            chunks_by_doc.get(doc_id, []),
            key=lambda r: (r.get("page") or 0, r.get("chunk_index") or 0),
        )
        visuals = visuals_by_doc.get(doc_id, [])
        metas = metadata_by_doc.get(doc_id, [])

        source = next(
            (r.get("source") for r in chunks + visuals + metas
             if isinstance(r.get("source"), str) and r.get("source")),
            doc_id,
        )
        extractor = next(
            (r.get("extractor") for r in chunks
             if isinstance(r.get("extractor"), str) and r.get("extractor")),
            "unknown",
        )
        fingerprint = make_parser_fingerprint(extractor, vision_model)

        artifact_uri, artifact_status, resolved = _resolve_artifact(
            source, kb_path, src_path
        )
        all_records = chunks + visuals + metas
        content_hash = _content_hash(resolved, all_records)
        version = make_document_version(content_hash, fingerprint)

        evidence_items: List[Evidence] = []
        pages: Dict[int, Page] = {}
        figures: List[Figure] = []
        metadata_fields: Dict[str, Any] = {}

        # --- text chunks ---------------------------------------------------
        # Neighbours are positional in the document-wide numeric order
        # (page, chunk_index). chunk_index resets per page upstream, so it
        # must never be used as a global lookup key.
        for pos, row in enumerate(chunks):
            page = row.get("page") if isinstance(row.get("page"), int) else None
            chunk_id = row.get("chunk_id")
            degraded = False
            reasons: List[str] = []
            if page is None or not row.get("source"):
                degraded = True
                reasons.append("unlocatable_source_page")
            prev_id = chunks[pos - 1].get("chunk_id") if pos > 0 else None
            next_id = chunks[pos + 1].get("chunk_id") if pos + 1 < len(chunks) else None
            prev_id = prev_id if isinstance(prev_id, str) else None
            next_id = next_id if isinstance(next_id, str) else None
            chunk_index = row.get("chunk_index")
            evidence_items.append(Evidence(
                evidence_id=make_evidence_id(
                    workspace_id, version, chunk_id or f"chunk:{chunk_index}"
                ),
                workspace_id=workspace_id,
                document_id=doc_id,
                document_version=version,
                content_hash=content_hash,
                page=page,
                section=row.get("section") if isinstance(row.get("section"), str) else None,
                modality=Modality.TEXT,
                source_span=SourceSpan(
                    chunk_id=chunk_id if isinstance(chunk_id, str) else None,
                    prev_chunk_id=prev_id,
                    next_chunk_id=next_id,
                ),
                artifact_uri=artifact_uri,
                parser_fingerprint=fingerprint,
                authority_level="primary",
                degraded=degraded,
                degraded_reason="; ".join(reasons) or None,
                content=row.get("content") if isinstance(row.get("content"), str) else None,
            ))
            if page is not None:
                pg = pages.setdefault(page, Page(page=page))
                if isinstance(chunk_id, str):
                    pg.chunk_ids.append(chunk_id)

        # --- visual records -------------------------------------------------
        for idx, row in enumerate(visuals):
            page = row.get("page") if isinstance(row.get("page"), int) else None
            figure_id = row.get("figure_id") if isinstance(row.get("figure_id"), str) else None
            bbox = _parse_bbox(row.get("region_bbox") or row.get("bbox"))
            reasons = []
            if bbox is None:
                reasons.append("missing_region_bbox")
            if figure_id is None:
                reasons.append("missing_figure_id")
            if page is None or not row.get("source"):
                reasons.append("unlocatable_source_page")
            degraded = bool(reasons)
            description = row.get("description")
            locator = figure_id or f"figure-row:{idx}"
            evidence_items.append(Evidence(
                evidence_id=make_evidence_id(workspace_id, version, locator),
                workspace_id=workspace_id,
                document_id=doc_id,
                document_version=version,
                content_hash=content_hash,
                page=page,
                figure_id=figure_id,
                region_bbox=bbox,
                modality=Modality.VISUAL,
                source_span=SourceSpan(
                    figure_index=row.get("figure_index")
                    if isinstance(row.get("figure_index"), int) else None,
                ),
                artifact_uri=artifact_uri,
                parser_fingerprint=fingerprint,
                authority_level="supporting",
                degraded=degraded,
                degraded_reason="; ".join(reasons) or None,
                content=description if isinstance(description, str) else None,
            ))
            figures.append(Figure(
                figure_id=figure_id,
                page=page,
                region_bbox=bbox,
                description=description if isinstance(description, str) else None,
                degraded=degraded,
                degraded_reason="; ".join(reasons) or None,
            ))
            if page is not None and figure_id:
                pages.setdefault(page, Page(page=page)).figure_ids.append(figure_id)

        # --- metadata records ----------------------------------------------
        for idx, row in enumerate(metas):
            error = row.get("_error")
            fields = {
                k: v for k, v in row.items()
                if k not in {"source", "document_id", "_error"}
            }
            if not error:
                metadata_fields.update(fields)
            evidence_items.append(Evidence(
                evidence_id=make_evidence_id(workspace_id, version, f"metadata:{idx}"),
                workspace_id=workspace_id,
                document_id=doc_id,
                document_version=version,
                content_hash=content_hash,
                modality=Modality.METADATA,
                artifact_uri=artifact_uri,
                parser_fingerprint=fingerprint,
                authority_level="supporting",
                degraded=bool(error),
                degraded_reason=(
                    f"metadata_extraction_error: {error}" if error else None
                ),
                content=json.dumps(fields or {"_error": error}, ensure_ascii=False)
                if (fields or error) else None,
                metadata_fields=fields,
            ))

        document = Document(
            document_id=doc_id,
            workspace_id=workspace_id,
            source=source,
            content_hash=content_hash,
            document_version=version,
            parser_fingerprint=fingerprint,
            artifact_uri=artifact_uri,
            artifact_status=artifact_status,
            page_count=len(pages) or None,
            pages=[pages[p] for p in sorted(pages)],
            figures=figures,
            metadata_fields=metadata_fields,
        )
        result.documents.append(ParsedDocument(document=document, evidence=evidence_items))

    return result
