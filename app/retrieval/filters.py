"""Metadata filters applied to fused candidates.

Every dropped candidate records a human-readable reason on the candidate
itself (``filter_reasons``) and in the returned report, so the trace always
explains why a recall-path hit was removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .types import Candidate


@dataclass
class FilterConfig:
    workspace_id: Optional[str] = None
    document_version: Optional[str] = None
    allowed_authority_levels: Optional[List[str]] = None  # None = allow all
    allowed_modalities: Optional[List[str]] = None        # None = allow all
    as_of: Optional[str] = None  # ISO ts; keep evidence valid at this instant
    exclude_degraded: bool = True


def _check(cand: Candidate, ev: Dict[str, Any],
           cfg: FilterConfig) -> List[str]:
    reasons: List[str] = []
    if ev.get("unresolved"):
        return ["no_evidence_record: candidate has no row in the evidence store"]
    if cfg.exclude_degraded and ev.get("degraded"):
        reasons.append(f"degraded: {ev.get('degraded_reason') or 'flagged'}")
    if cfg.workspace_id is not None and ev.get("workspace_id") != cfg.workspace_id:
        reasons.append(
            f"workspace_mismatch: {ev.get('workspace_id')!r} != {cfg.workspace_id!r}")
    if (cfg.document_version is not None
            and ev.get("document_version") != cfg.document_version):
        reasons.append(
            f"document_version_mismatch: {ev.get('document_version')!r}"
            f" != {cfg.document_version!r}")
    if (cfg.allowed_authority_levels is not None
            and ev.get("authority_level") not in cfg.allowed_authority_levels):
        reasons.append(
            f"authority_level_excluded: {ev.get('authority_level')!r}"
            f" not in {cfg.allowed_authority_levels}")
    if (cfg.allowed_modalities is not None
            and ev.get("modality") not in cfg.allowed_modalities):
        reasons.append(
            f"modality_excluded: {ev.get('modality')!r}"
            f" not in {cfg.allowed_modalities}")
    if cfg.as_of is not None:
        vf, vt = ev.get("valid_from"), ev.get("valid_to")
        if vf and cfg.as_of < vf:
            reasons.append(f"not_yet_valid: valid_from {vf} > as_of {cfg.as_of}")
        if vt and cfg.as_of >= vt:
            reasons.append(f"expired: valid_to {vt} <= as_of {cfg.as_of}")
    return reasons


def apply_filters(
    candidates: List[Candidate],
    evidence_by_id: Dict[str, Dict[str, Any]],
    cfg: FilterConfig,
) -> Tuple[List[Candidate], List[Dict[str, Any]]]:
    """Split candidates into (kept, dropped-report). Input order preserved."""
    kept: List[Candidate] = []
    dropped: List[Dict[str, Any]] = []
    for cand in candidates:
        ev = evidence_by_id.get(cand.evidence_id, {"unresolved": True})
        reasons = _check(cand, ev, cfg)
        if reasons:
            cand.kept = False
            cand.filter_reasons.extend(reasons)
            dropped.append({
                "evidence_id": cand.evidence_id,
                "rank_before_filter": cand.rank,
                "reasons": reasons,
            })
        else:
            kept.append(cand)
    for rank, cand in enumerate(kept, start=1):
        cand.rank = rank
    return kept, dropped
