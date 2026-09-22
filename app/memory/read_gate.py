"""RADIANT-Control M6: Read Gate.

Default recall is conservative:

* strict workspace isolation -- records from other workspaces are never
  returned (cross-workspace leakage must be 0), even for similar subjects;
* superseded records are excluded (they stay queryable via the store /
  audit trail, never via default recall);
* expired records (TTL elapsed or valid_to passed) are excluded, so the
  stale-hit rate of default recall is 0;
* read-only namespaces (skills/policy) ARE readable -- they are read-only
  for *writes*, and serve as policy context on reads;
* task relevance: when the query carries text/subjects, a record must share
  token overlap (subject weighted 2x over value) above ``min_relevance``;
  this filters same-workspace cross-session topic bleed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.memory.models import MemoryRecord

_CJK_RUN = re.compile(r"[一-鿿]+")
_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    """Latin word tokens plus CJK unigrams/bigrams (offline, deterministic)."""
    text = text.lower()
    tokens = set(_WORD.findall(text))
    for run in _CJK_RUN.findall(text):
        tokens.update(run)
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def relevance(query_tokens: set[str], record: MemoryRecord) -> float:
    if not query_tokens:
        return 0.0
    subject_tokens = tokenize(record.subject)
    value_tokens = tokenize(record.value)
    overlap = 2 * len(query_tokens & subject_tokens) + len(query_tokens & value_tokens)
    return overlap / (2 * len(query_tokens))


@dataclass
class ReadQuery:
    workspace: str
    text: str = ""
    subjects: list[str] = field(default_factory=list)
    namespace: Optional[str] = None
    category: Optional[str] = None
    top_k: int = 5
    min_relevance: float = 0.1


class ReadGate:
    def __init__(self, *, now=None) -> None:
        import time

        self._now = now or time.time

    def recall(self, store, query: ReadQuery, *, now: float | None = None) -> list[MemoryRecord]:
        now = self._now() if now is None else now
        records = store.list_records(
            workspace=query.workspace,
            namespace=query.namespace,
            category=query.category,
            include_superseded=False,
        )
        records = [r for r in records if not r.is_expired(now)]

        query_text = " ".join([query.text, *query.subjects]).strip()
        if query_text:
            q_tokens = tokenize(query_text)
            scored = [(relevance(q_tokens, r), r) for r in records]
            scored = [(s, r) for s, r in scored if s >= query.min_relevance and s > 0.0]
            scored.sort(key=lambda sr: (-sr[0], -sr[1].created_at))
            return [r for _, r in scored[: query.top_k]]

        records.sort(key=lambda r: -r.created_at)
        return records[: query.top_k]
