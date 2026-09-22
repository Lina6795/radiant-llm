"""RADIANT-Control M7: visual parse QA checker.

Rule-based QA over ``02_visuals_kb.jsonl`` records as written by
``app/utils/vp_figure_describer.py`` (see its module docstring):

    {"source", "page", "document_id", "figure_index", "figure_id",
     "description"}   with figure_id == "{document_id}:p{page}:f{figure_index}"

Detected defect classes (the attribution layers for visual failures):
  * empty_description   — VLM returned no usable text
  * wrong_page          — page missing/<=0, beyond the document page count,
                          or inconsistent with the figure_id page component
  * wrong_figure_index  — figure_index missing, inconsistent with the
                          figure_id index component, malformed figure_id,
                          or a duplicated figure_id within the batch
  * low_information     — description too short, too low in character
                          entropy, or claims quantitative content (chart /
                          axis / plot) with zero digit density

Framework + synthetic fixtures only. Real VLM output QA is deferred until
the vision endpoint key is available (see docs/CLAIM_VERIFICATION.md).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field

FIGURE_ID_RE = re.compile(r"^(?P<doc>[0-9a-zA-Z]+):p(?P<page>\d+):f(?P<idx>\d+)$")

_QUANT_MARKERS = ("chart", "axis", "axes", "plot", "curve", "bar", "scatter", "heatmap")


class IssueType(str, Enum):
    EMPTY_DESCRIPTION = "empty_description"
    WRONG_PAGE = "wrong_page"
    WRONG_FIGURE_INDEX = "wrong_figure_index"
    LOW_INFORMATION = "low_information"


class VisualIssue(BaseModel):
    record_key: str
    issue_type: IssueType
    detail: str


class VisualQAReport(BaseModel):
    records_checked: int
    issues: List[VisualIssue] = Field(default_factory=list)
    issue_counts: Dict[str, int] = Field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# Description-quality primitives
# ---------------------------------------------------------------------------

def description_entropy(text: str) -> float:
    """Shannon entropy (bits) over the character distribution."""
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def digit_density(text: str) -> float:
    if not text:
        return 0.0
    return sum(ch.isdigit() for ch in text) / len(text)


def is_low_information(
    description: str,
    *,
    min_chars: int = 40,
    min_entropy: float = 3.0,
) -> tuple[bool, str]:
    """Low-information heuristic: length + character entropy + a digit-density
    rule for descriptions that claim quantitative content."""
    text = (description or "").strip()
    if len(text) < min_chars:
        return True, f"description too short ({len(text)} < {min_chars} chars)"
    ent = description_entropy(text)
    if ent < min_entropy:
        return True, f"character entropy {ent:.2f} bits < {min_entropy}"
    low = text.lower()
    if any(m in low for m in _QUANT_MARKERS) and digit_density(text) == 0.0:
        return True, "claims quantitative content but digit density is 0.0"
    return False, ""


# ---------------------------------------------------------------------------
# Record checks
# ---------------------------------------------------------------------------

def _record_key(rec: Dict[str, Any], index: int) -> str:
    return str(rec.get("figure_id") or f"{rec.get('source', '?')}#{index}")


def check_record(
    rec: Dict[str, Any],
    *,
    index: int = 0,
    page_count: Optional[int] = None,
    seen_figure_ids: Optional[set[str]] = None,
) -> List[VisualIssue]:
    key = _record_key(rec, index)
    issues: List[VisualIssue] = []

    description = (rec.get("description") or "").strip()
    if not description:
        issues.append(VisualIssue(
            record_key=key, issue_type=IssueType.EMPTY_DESCRIPTION,
            detail="figure description is empty",
        ))

    page = rec.get("page")
    if not isinstance(page, int) or page <= 0:
        issues.append(VisualIssue(
            record_key=key, issue_type=IssueType.WRONG_PAGE,
            detail=f"invalid page value: {page!r}",
        ))
    elif page_count is not None and page > page_count:
        issues.append(VisualIssue(
            record_key=key, issue_type=IssueType.WRONG_PAGE,
            detail=f"page {page} exceeds document page count {page_count}",
        ))

    figure_id = str(rec.get("figure_id") or "")
    match = FIGURE_ID_RE.match(figure_id)
    if not match:
        issues.append(VisualIssue(
            record_key=key, issue_type=IssueType.WRONG_FIGURE_INDEX,
            detail=f"malformed figure_id {figure_id!r} (expected '<doc>:p<page>:f<idx>')",
        ))
    else:
        fid_page, fid_idx = int(match.group("page")), int(match.group("idx"))
        if isinstance(page, int) and page > 0 and fid_page != page:
            issues.append(VisualIssue(
                record_key=key, issue_type=IssueType.WRONG_PAGE,
                detail=f"figure_id page p{fid_page} != record page {page}",
            ))
        if rec.get("figure_index") is None or rec.get("figure_index") != fid_idx:
            issues.append(VisualIssue(
                record_key=key, issue_type=IssueType.WRONG_FIGURE_INDEX,
                detail=f"figure_index {rec.get('figure_index')!r} != figure_id index f{fid_idx}",
            ))
        if seen_figure_ids is not None:
            if figure_id in seen_figure_ids:
                issues.append(VisualIssue(
                    record_key=key, issue_type=IssueType.WRONG_FIGURE_INDEX,
                    detail=f"duplicate figure_id {figure_id!r} within batch",
                ))
            seen_figure_ids.add(figure_id)

    low, why = is_low_information(description)
    if low:
        issues.append(VisualIssue(
            record_key=key, issue_type=IssueType.LOW_INFORMATION, detail=why,
        ))
    return issues


def check_records(
    records: Iterable[Dict[str, Any]],
    *,
    page_counts: Optional[Dict[str, int]] = None,
) -> VisualQAReport:
    """Batch QA. ``page_counts`` maps source document -> page count."""
    records = list(records)
    seen: set[str] = set()
    issues: List[VisualIssue] = []
    for idx, rec in enumerate(records):
        pc = (page_counts or {}).get(str(rec.get("source")))
        issues.extend(check_record(rec, index=idx, page_count=pc, seen_figure_ids=seen))
    counts: Dict[str, int] = {}
    for issue in issues:
        counts[issue.issue_type.value] = counts.get(issue.issue_type.value, 0) + 1
    return VisualQAReport(
        records_checked=len(records), issues=issues, issue_counts=counts
    )


def load_kb_records(kb_path: str) -> List[Dict[str, Any]]:
    import json

    with open(kb_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
