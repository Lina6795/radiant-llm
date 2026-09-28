"""S10: post-answer memory candidate generation.

After a run SUCCEEDS and the verifier ACCEPTS the answer, the API layer
offers conservative candidates to the Write Gate:

* one ``session`` record -- a short-TTL task summary of the answered run;
* ``evidence_pointer`` records -- one per evidence_id cited by a SUPPORTED
  claim, pointing back into app.evidence.

``user_fact`` and ``decision`` are NEVER auto-generated here: both require
explicit human confirmation (docs/MEMORY_GOVERNANCE.md: 禁止自动提升), and
the Write Gate would reject them with ``missing_user_confirmation`` anyway.

Generation is only a *proposal*. The Write Gate stays default-deny: anything
malformed, low-confidence, sensitive or conflicting is rejected and audited,
never stored. A rejected candidate is a normal outcome, not an error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.memory.models import (
    MemoryCandidate,
    MemoryCategory,
    Origin,
    Provenance,
    Sensitivity,
)

SESSION_TTL_SECONDS = 7 * 24 * 3600  # session summaries expire after 7 days
_ANSWER_VALUE_LIMIT = 500
_CLAIM_VALUE_LIMIT = 300


def _claim_text(claim: Dict[str, Any]) -> str:
    text = claim.get("text") or claim.get("claim") or ""
    if not text:
        page = claim.get("page")
        text = f"supported claim citing page {page}" if page is not None else "supported claim"
    return text[:_CLAIM_VALUE_LIMIT]


def candidates_from_run(
    *,
    run_id: str,
    goal: str,
    answer: str,
    claims: List[Dict[str, Any]],
    workspace: str = "default",
    namespace: str = "default",
) -> List[MemoryCandidate]:
    """Build the conservative post-answer candidate set for one accepted run.

    ``confidence`` is the run's supported-claim coverage; below the Write
    Gate threshold the candidate is simply rejected (fail-closed, audited).
    """
    supported = [c for c in claims if c.get("verdict") == "supported"]
    coverage = round(len(supported) / len(claims), 3) if claims else 0.5

    candidates: List[MemoryCandidate] = []

    summary_value = (answer or "").strip()[:_ANSWER_VALUE_LIMIT]
    if summary_value:
        candidates.append(MemoryCandidate(
            category=MemoryCategory.SESSION,
            subject=f"run_summary:{run_id}",
            value=summary_value,
            namespace=namespace,
            workspace=workspace,
            provenance=Provenance(origin=Origin.SYSTEM, source_run_id=run_id),
            write_reason="post_answer:auto session summary of accepted run",
            confidence=coverage,
            sensitivity=Sensitivity.INTERNAL,
            ttl_seconds=SESSION_TTL_SECONDS,
        ))

    seen: set[str] = set()
    for claim in supported:
        for evidence_id in (claim.get("evidence_ids") or []):
            if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen:
                continue
            seen.add(evidence_id)
            candidates.append(MemoryCandidate(
                category=MemoryCategory.EVIDENCE_POINTER,
                subject=f"evidence_cited:{evidence_id}",
                value=_claim_text(claim),
                namespace=namespace,
                workspace=workspace,
                provenance=Provenance(
                    origin=Origin.SYSTEM,
                    source_run_id=run_id,
                    evidence_id=evidence_id,
                ),
                write_reason="post_answer:auto pointer to evidence cited by supported claim",
                confidence=coverage,
                sensitivity=Sensitivity.INTERNAL,
            ))
    return candidates
