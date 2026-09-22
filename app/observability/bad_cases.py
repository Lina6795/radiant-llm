"""Bad-case Registry (M8 data flywheel底座).

JSON-backed registry for the flywheel loop

    发现 -> 归因 -> 固定为 regression case -> 修复 -> 复验 -> 关闭

Each entry records: id, discovery date, source, observed symptom, the
layer the failure is attributed to, root cause, the fix commit, the
regression case that freezes the failure, and a status
(``open`` -> ``fixing`` -> ``fixed`` -> ``verified``).

``export_ledger`` renders the registry as a Markdown table suitable for
pasting into the project ledger (台账); the registry itself is the
machine-readable source of truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUSES = ("open", "fixing", "fixed", "verified")
SCHEMA_VERSION = "radiant-badcase/v1"


@dataclass
class BadCase:
    bad_case_id: str                 # e.g. "BC-retrieval-004"
    found_date: str                  # ISO date, e.g. "2026-09-22"
    source: str                      # where found: trace / review / user feedback / audit
    symptom: str                     # observed phenomenon
    attribution_layer: str           # layer the failure is attributed to
    root_cause: str = ""
    fix_commit: Optional[str] = None
    regression_case: Optional[str] = None  # frozen case id or test reference
    status: str = "open"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")
        if not self.attribution_layer:
            raise ValueError("attribution_layer is required (failures must be attributed)")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BadCase":
        payload = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**payload)


class BadCaseRegistry:
    """JSON-file-backed registry. One file, a list of entries."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._cases: Dict[str, BadCase] = {}
        if self.path.is_file():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"{self.path}: unsupported bad-case registry schema")
        for entry in data.get("cases", []):
            case = BadCase.from_dict(entry)
            self._cases[case.bad_case_id] = case

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "cases": [c.to_dict() for c in sorted(
                self._cases.values(), key=lambda c: c.bad_case_id)],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    # ------------------------------------------------------------------
    def add(self, case: BadCase, *, overwrite: bool = False) -> BadCase:
        if case.bad_case_id in self._cases and not overwrite:
            raise ValueError(f"bad case {case.bad_case_id} already registered")
        self._cases[case.bad_case_id] = case
        self.save()
        return case

    def update(self, bad_case_id: str, **fields: Any) -> BadCase:
        case = self._cases.get(bad_case_id)
        if case is None:
            raise KeyError(f"unknown bad case {bad_case_id}")
        unknown = set(fields) - set(BadCase.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown bad-case fields: {sorted(unknown)}")
        updated = BadCase.from_dict({**case.to_dict(), **fields})
        self._cases[bad_case_id] = updated
        self.save()
        return updated

    def get(self, bad_case_id: str) -> Optional[BadCase]:
        return self._cases.get(bad_case_id)

    def list(self, *, status: Optional[str] = None,
             layer: Optional[str] = None) -> List[BadCase]:
        cases = sorted(self._cases.values(), key=lambda c: c.bad_case_id)
        if status:
            cases = [c for c in cases if c.status == status]
        if layer:
            cases = [c for c in cases if c.attribution_layer == layer]
        return cases

    # ------------------------------------------------------------------
    def export_ledger(self) -> str:
        """Markdown table for the project ledger (台账导出接口)."""
        lines = [
            "| ID | 发现日期 | 来源 | 现象 | 归因层 | root cause | 修复 commit | regression case | 状态 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in self.list():
            lines.append(
                f"| {c.bad_case_id} | {c.found_date} | {c.source} | {c.symptom} "
                f"| {c.attribution_layer} | {c.root_cause or '—'} "
                f"| {c.fix_commit or '—'} | {c.regression_case or '—'} | {c.status} |")
        return "\n".join(lines) + "\n"


def new_case(registry: BadCaseRegistry, *, layer: str, source: str,
             symptom: str, sequence: Optional[int] = None,
             **kwargs: Any) -> BadCase:
    """Convenience constructor with a sequential id and today's date."""
    if sequence is None:
        existing = [c for c in registry.list() if c.attribution_layer == layer]
        sequence = len(existing) + 1
    return BadCase(
        bad_case_id=f"BC-{layer}-{sequence:03d}",
        found_date=date.today().isoformat(),
        source=source, symptom=symptom, attribution_layer=layer, **kwargs)
