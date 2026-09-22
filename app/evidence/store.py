"""
SQLite evidence store with document-level idempotent ingestion.

Guarantees:
- Re-ingesting a document with the same content_hash + parser_fingerprint is
  a short-circuit no-op (zero new rows) — the adapter layer implements real
  idempotency that the upstream pipeline (BC-parser-002) lacks.
- Changed content yields a new document_version; old versions stay queryable.
- Per-document failure isolation: one broken document never aborts the others.
- Resumable ingestion: every document version is tracked in ``ingest_state``
  (pending/done/failed); a re-run only retries pending/failed work.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapter import ParsedDocument, parse_kb_directory
from .models import Evidence, IngestStatus, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parser_fingerprint TEXT NOT NULL,
    source TEXT,
    artifact_uri TEXT,
    artifact_status TEXT NOT NULL DEFAULT 'ok',
    page_count INTEGER,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    UNIQUE (workspace_id, document_id, document_version)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    document_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    modality TEXT NOT NULL,
    page INTEGER,
    section TEXT,
    figure_id TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    degraded_reason TEXT,
    authority_level TEXT,
    artifact_uri TEXT,
    parser_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_doc ON evidence (workspace_id, document_id);
CREATE INDEX IF NOT EXISTS idx_evidence_modality ON evidence (modality);
CREATE INDEX IF NOT EXISTS idx_evidence_degraded ON evidence (degraded);
CREATE TABLE IF NOT EXISTS ingest_state (
    workspace_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    parser_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, document_id, content_hash, parser_fingerprint)
);
"""

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def default_db_path() -> Path:
    override = os.getenv("RADIANT_EVIDENCE_DB")
    if override:
        return Path(override)
    config_dir = os.getenv("RADIANT_LLM_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.cwd()
    return base / "evidence.db"


def get_evidence_store(db_path: Optional[str | os.PathLike[str]] = None) -> "EvidenceStore":
    """
    Store factory. Resolution order: explicit argument, RADIANT_EVIDENCE_DB,
    RADIANT_LLM_CONFIG_DIR/evidence.db, ./evidence.db. No hardcoded paths.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    return EvidenceStore(path)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


class EvidenceStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _state_key(self, parsed: ParsedDocument) -> tuple:
        doc = parsed.document
        return (doc.workspace_id, doc.document_id, doc.content_hash,
                doc.parser_fingerprint)

    def _get_state(self, parsed: ParsedDocument) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM ingest_state WHERE workspace_id=? AND document_id=?"
            " AND content_hash=? AND parser_fingerprint=?",
            self._state_key(parsed),
        ).fetchone()

    def _set_state(self, parsed: ParsedDocument, status: IngestStatus,
                   error: Optional[str] = None) -> None:
        key = self._state_key(parsed)
        self._conn.execute(
            "INSERT INTO ingest_state (workspace_id, document_id, content_hash,"
            " parser_fingerprint, status, error, attempts, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(workspace_id, document_id, content_hash, parser_fingerprint)"
            " DO UPDATE SET status=excluded.status, error=excluded.error,"
            " attempts=ingest_state.attempts+1, updated_at=excluded.updated_at",
            (*key, status.value, error, 1, utcnow().isoformat()),
        )

    def _insert_bundle(self, parsed: ParsedDocument) -> int:
        """Insert one document version + its evidence. Returns rows inserted."""
        doc = parsed.document
        now = utcnow()
        doc.valid_from = now
        cur = self._conn.cursor()
        # Supersede previous versions (kept queryable, validity window closed).
        cur.execute(
            "UPDATE documents SET valid_to=? WHERE workspace_id=? AND document_id=?"
            " AND document_version<>? AND valid_to IS NULL",
            (_iso(now), doc.workspace_id, doc.document_id, doc.document_version),
        )
        cur.execute(
            "UPDATE evidence SET valid_to=? WHERE workspace_id=? AND document_id=?"
            " AND document_version<>? AND valid_to IS NULL",
            (_iso(now), doc.workspace_id, doc.document_id, doc.document_version),
        )
        cur.execute(
            "INSERT INTO documents (workspace_id, document_id, document_version,"
            " content_hash, parser_fingerprint, source, artifact_uri,"
            " artifact_status, page_count, payload, valid_from, valid_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc.workspace_id, doc.document_id, doc.document_version,
             doc.content_hash, doc.parser_fingerprint, doc.source,
             doc.artifact_uri, doc.artifact_status, doc.page_count,
             doc.model_dump_json(), _iso(doc.valid_from), _iso(doc.valid_to)),
        )
        inserted = 1
        for item in parsed.evidence:
            item.valid_from = now
            cur.execute(
                "INSERT INTO evidence (evidence_id, workspace_id, document_id,"
                " document_version, content_hash, modality, page, section,"
                " figure_id, degraded, degraded_reason, authority_level,"
                " artifact_uri, parser_fingerprint, payload, valid_from, valid_to)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.evidence_id, item.workspace_id, item.document_id,
                 item.document_version, item.content_hash, item.modality.value,
                 item.page, item.section, item.figure_id, int(item.degraded),
                 item.degraded_reason, item.authority_level, item.artifact_uri,
                 item.parser_fingerprint, item.model_dump_json(),
                 _iso(item.valid_from), _iso(item.valid_to)),
            )
            inserted += 1
        return inserted

    def ingest_parsed(self, parsed: ParsedDocument) -> Dict[str, Any]:
        """
        Idempotently ingest one parsed document. Failures are recorded in
        ingest_state and returned, never raised past the document boundary.
        """
        doc = parsed.document
        state = self._get_state(parsed)
        if state is not None and state["status"] == IngestStatus.DONE.value:
            return {
                "document_id": doc.document_id,
                "document_version": doc.document_version,
                "status": IngestStatus.DONE.value,
                "short_circuited": True,
                "new_records": 0,
                "error": None,
            }
        try:
            self._set_state(parsed, IngestStatus.PENDING)
            with self._conn:
                new_records = self._insert_bundle(parsed)
                self._set_state(parsed, IngestStatus.DONE)
            return {
                "document_id": doc.document_id,
                "document_version": doc.document_version,
                "status": IngestStatus.DONE.value,
                "short_circuited": False,
                "new_records": new_records,
                "error": None,
            }
        except Exception as exc:  # failure isolation: recorded, not raised
            with self._conn:
                self._set_state(parsed, IngestStatus.FAILED, error=str(exc))
            return {
                "document_id": doc.document_id,
                "document_version": doc.document_version,
                "status": IngestStatus.FAILED.value,
                "short_circuited": False,
                "new_records": 0,
                "error": str(exc),
            }

    def ingest_directory(
        self,
        kb_dir: str | os.PathLike[str],
        workspace_id: str = "default",
        source_dir: Optional[str | os.PathLike[str]] = None,
        vision_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parse a KB output directory and ingest every document found."""
        result = parse_kb_directory(
            kb_dir, workspace_id=workspace_id,
            source_dir=source_dir, vision_model=vision_model,
        )
        outcomes = [self.ingest_parsed(parsed) for parsed in result.documents]
        return {
            "workspace_id": workspace_id,
            "documents": outcomes,
            "rejected_rows": result.rejected_rows,
            "new_records": sum(o["new_records"] for o in outcomes),
            "failed": [o["document_id"] for o in outcomes
                       if o["status"] == IngestStatus.FAILED.value],
        }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @staticmethod
    def _row_payload(row: sqlite3.Row) -> Dict[str, Any]:
        return json.loads(row["payload"])

    def query_evidence(
        self,
        workspace_id: Optional[str] = None,
        document_id: Optional[str] = None,
        modality: Optional[str] = None,
        degraded: Optional[bool] = None,
        include_degraded: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        List evidence. Degraded records are excluded by default; pass
        ``include_degraded=True`` (or an explicit ``degraded`` filter) to
        see them — degraded evidence is never an authoritative-answer
        candidate unless explicitly requested.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if modality is not None:
            clauses.append("modality = ?")
            params.append(modality)
        if degraded is not None:
            clauses.append("degraded = ?")
            params.append(int(degraded))
        elif not include_degraded:
            clauses.append("degraded = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM evidence {where}", params
        ).fetchone()["n"]
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        rows = self._conn.execute(
            f"SELECT payload FROM evidence {where}"
            " ORDER BY document_id, page, evidence_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._row_payload(r) for r in rows],
        }

    def get_evidence(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT payload FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        return self._row_payload(row) if row else None

    def list_documents(
        self,
        workspace_id: Optional[str] = None,
        document_id: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List all document versions, newest first."""
        clauses: List[str] = []
        params: List[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM documents {where}", params
        ).fetchone()["n"]
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        rows = self._conn.execute(
            f"SELECT payload FROM documents {where}"
            " ORDER BY document_id, valid_from DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._row_payload(r) for r in rows],
        }

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------

    def verify_provenance(
        self, workspace_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Check every recorded artifact_uri against the filesystem. Returns an
        explicit per-version report; a missing artifact is reported with the
        URI and expected path — never silently ignored.
        """
        clauses = ""
        params: List[Any] = []
        if workspace_id is not None:
            clauses = "WHERE workspace_id = ?"
            params.append(workspace_id)
        rows = self._conn.execute(
            f"SELECT workspace_id, document_id, document_version, artifact_uri,"
            f" artifact_status FROM documents {clauses}", params
        ).fetchall()
        report: List[Dict[str, Any]] = []
        for row in rows:
            uri = row["artifact_uri"] or ""
            path = uri[len("file://"):] if uri.startswith("file://") else uri
            exists = bool(path) and path != "unknown" and os.path.exists(path)
            report.append({
                "workspace_id": row["workspace_id"],
                "document_id": row["document_id"],
                "document_version": row["document_version"],
                "artifact_uri": uri,
                "status": "ok" if exists else "missing",
                "detail": (
                    "artifact resolves on disk" if exists else
                    f"artifact_uri unresolvable: no file at {path!r} "
                    "(original may have been moved or deleted)"
                ),
            })
        return report
