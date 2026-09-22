"""
Evidence schema for RADIANT-Control Milestone M1.

Identity rules (see docs/adr/0002-evidence-identity.md):
- ``content_hash`` is derived from the source artifact bytes when resolvable,
  otherwise from the canonical JSON of the parsed records.
- ``document_version`` is derived from ``content_hash`` — same filename with
  different content yields a new version; old versions are never overwritten.
- ``parser_fingerprint`` captures the extraction stack (extractor +
  VLM model + adapter version) so evidence produced by different parsers
  is never conflated.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

ADAPTER_VERSION = "evidence-adapter/1"


class Modality(str, Enum):
    TEXT = "text"
    VISUAL = "visual"
    METADATA = "metadata"


class IngestStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_document_version(content_hash: str, parser_fingerprint: str = "") -> str:
    """
    Derive a document version string from the content hash. The parser
    fingerprint is folded in as a short suffix so the same bytes parsed by a
    different extraction stack land in a distinct version (evidence identity
    = content_hash + parser_fingerprint).
    """
    base = f"v-{content_hash[:12]}"
    if parser_fingerprint:
        suffix = hashlib.sha256(parser_fingerprint.encode("utf-8")).hexdigest()[:6]
        return f"{base}-{suffix}"
    return base


def make_parser_fingerprint(extractor: str, vision_model: Optional[str]) -> str:
    """
    Fingerprint of the extraction stack. Distinguishes the text extractor
    (nougat / lightweight / unknown) from the VLM model used for visual and
    metadata records.
    """
    vlm = vision_model or "unknown"
    return f"{ADAPTER_VERSION};extractor={extractor or 'unknown'};vlm={vlm}"


def make_evidence_id(workspace_id: str, document_version: str, locator: str) -> str:
    digest = hashlib.sha256(
        f"{workspace_id}|{document_version}|{locator}".encode("utf-8")
    ).hexdigest()
    return f"ev-{digest[:24]}"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RegionBBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float
    unit: str = "pt"


class SourceSpan(BaseModel):
    """Locator of the evidence inside the parsed artifact."""

    chunk_id: Optional[str] = None
    prev_chunk_id: Optional[str] = None
    next_chunk_id: Optional[str] = None
    figure_index: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None


class Evidence(BaseModel):
    evidence_id: str
    workspace_id: str
    document_id: str
    document_version: str
    content_hash: str
    page: Optional[int] = None
    section: Optional[str] = None
    figure_id: Optional[str] = None
    region_bbox: Optional[RegionBBox] = None
    modality: Modality
    source_span: Optional[SourceSpan] = None
    artifact_uri: str
    parser_fingerprint: str
    authority_level: str = "primary"
    valid_from: datetime = Field(default_factory=utcnow)
    valid_to: Optional[datetime] = None
    degraded: bool = False
    degraded_reason: Optional[str] = None
    content: Optional[str] = None
    metadata_fields: Dict[str, Any] = Field(default_factory=dict)


class Page(BaseModel):
    page: int
    chunk_ids: List[str] = Field(default_factory=list)
    figure_ids: List[str] = Field(default_factory=list)


class Figure(BaseModel):
    figure_id: Optional[str] = None
    page: Optional[int] = None
    region_bbox: Optional[RegionBBox] = None
    description: Optional[str] = None
    degraded: bool = False
    degraded_reason: Optional[str] = None


class Document(BaseModel):
    document_id: str
    workspace_id: str
    source: str
    content_hash: str
    document_version: str
    parser_fingerprint: str
    artifact_uri: str
    artifact_status: str = "ok"  # "ok" | "missing"
    page_count: Optional[int] = None
    pages: List[Page] = Field(default_factory=list)
    figures: List[Figure] = Field(default_factory=list)
    metadata_fields: Dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime = Field(default_factory=utcnow)
    valid_to: Optional[datetime] = None
