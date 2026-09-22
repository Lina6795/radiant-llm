"""RADIANT-Control M1: stable evidence schema, adapter, and SQLite store."""

from .adapter import AdapterResult, ParsedDocument, make_document_id, parse_kb_directory
from .models import (
    ADAPTER_VERSION,
    Document,
    Evidence,
    Figure,
    IngestStatus,
    Modality,
    Page,
    RegionBBox,
    SourceSpan,
    make_document_version,
    make_evidence_id,
    make_parser_fingerprint,
)
from .store import EvidenceStore, default_db_path, get_evidence_store

__all__ = [
    "ADAPTER_VERSION",
    "AdapterResult",
    "Document",
    "Evidence",
    "EvidenceStore",
    "Figure",
    "IngestStatus",
    "Modality",
    "Page",
    "ParsedDocument",
    "RegionBBox",
    "SourceSpan",
    "default_db_path",
    "get_evidence_store",
    "make_document_id",
    "make_document_version",
    "make_evidence_id",
    "make_parser_fingerprint",
    "parse_kb_directory",
]
