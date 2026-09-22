"""The five paper metrics (CoP/CiP/CiH/HR/ViR) plus the judge protocol.

Metric families (see docs/EVAL_HARNESS.md):

* deterministic -- computed from data alone (CiP, CiH, and the numeric
  fidelity term of CoP);
* judge-based -- require an injected judge callable (the semantic grade
  of CoP, per-claim support for HR, answer quality);
* human audit -- manual scores entered through :func:`manual_scores`;
* operational -- latency / cost, collected by the runner, not here.

Judge red lines
---------------
* A judge is always an *injected* callable; this module never picks a
  default model and never calls an LLM by itself.
* Every judged score carries provenance: judge model id, the sha256 of
  the prompt template, and the number of repeats (raw scores kept).
* Without a judge, judge-based metrics are explicitly ``not_measured``
  (value ``None``). They are NEVER defaulted to a perfect score.
* Judge-based metrics are never labelled fully objective: reports must
  attach a human-audit consistency field before the release gate treats
  them as releasable.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Hashable, List, Optional, Sequence

# Semantic discrete grades for CoP (paper definition).
SEMANTIC_GRADES = (0.0, 0.25, 0.5, 0.75, 1.0)

DEFAULT_ALPHA = 0.6  # weight of the semantic grade inside CoP
DEFAULT_EPS = 1e-9

JudgeFn = Callable[[str], float]
"""An injected judge: receives one rendered prompt, returns a score in [0, 1]."""


# ---------------------------------------------------------------------------
# Deterministic metrics
# ---------------------------------------------------------------------------

def numeric_fidelity(value: float, gold_value: float, eps: float = DEFAULT_EPS) -> float:
    """``1 - |v - v*| / max(|v*|, eps)`` clipped to [0, 1]."""
    fidelity = 1.0 - abs(value - gold_value) / max(abs(gold_value), eps)
    return min(1.0, max(0.0, fidelity))


def cop(
    semantic_grade: float,
    value: Optional[float] = None,
    gold_value: Optional[float] = None,
    *,
    alpha: float = DEFAULT_ALPHA,
    eps: float = DEFAULT_EPS,
) -> float:
    """Correctness of Production (CoP).

    ``semantic_grade`` must be one of the discrete grades
    {0, 0.25, 0.5, 0.75, 1}. When a numeric answer and its gold value are
    supplied, CoP blends the semantic grade with the numeric fidelity
    term: ``alpha * semantic + (1 - alpha) * fidelity``. ``alpha`` is
    configurable (default 0.6).
    """
    if semantic_grade not in SEMANTIC_GRADES:
        raise ValueError(
            f"semantic_grade must be one of {SEMANTIC_GRADES}, got {semantic_grade!r}"
        )
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha!r}")
    if value is None or gold_value is None:
        return float(semantic_grade)
    return alpha * semantic_grade + (1.0 - alpha) * numeric_fidelity(
        value, gold_value, eps
    )


def cip(supported: Sequence[bool]) -> Optional[float]:
    """Citation Precision: effective (supported) citations / total citations.

    ``None`` when there are no citations (nothing measured).
    """
    if not supported:
        return None
    return sum(1 for s in supported if s) / len(supported)


def cih(anchor_hits: Sequence[bool]) -> Optional[float]:
    """Citation/anchor Hit: binary anchor hit, averaged over cases."""
    if not anchor_hits:
        return None
    return sum(1 for h in anchor_hits if h) / len(anchor_hits)


def hr(supported: Sequence[bool]) -> Optional[float]:
    """Hallucination Rate: unsupported claims / total claims.

    ``None`` when there are no claims (nothing measured).
    """
    if not supported:
        return None
    return sum(1 for s in supported if not s) / len(supported)


def vir(
    gold_facts: Sequence[Hashable], recalled_facts: Sequence[Hashable]
) -> Optional[float]:
    """Visual-fact Recall: recalled gold visual facts / total gold visual facts.

    ``None`` when the case has no gold visual facts (nothing measured).
    """
    gold = set(gold_facts)
    if not gold:
        return None
    return len(gold & set(recalled_facts)) / len(gold)


# ---------------------------------------------------------------------------
# Judge protocol with mandatory provenance
# ---------------------------------------------------------------------------

@dataclass
class JudgeProvenance:
    """Who judged, with which prompt, how many times."""

    judge_model: str              # e.g. "deepseek-v4-pro" or "human:<rater>"
    judge_kind: str               # "model" | "human"
    prompt_template_hash: str     # sha256 of the prompt template text
    n_repeats: int                # judged calls per item
    raw_scores: List[List[float]] = field(default_factory=list)  # per item, per repeat
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MetricResult:
    """One metric value as reported by the eval runner."""

    name: str
    value: Optional[float]
    status: str                    # "measured" | "not_measured"
    kind: str                      # "deterministic" | "judge" | "human_audit" | "operational"
    n_cases: int = 0
    reason: Optional[str] = None   # why not_measured
    judge: Optional[dict] = None   # JudgeProvenance.to_dict()
    human_review: Optional[dict] = None  # human-audit consistency (agreement etc.)

    def to_dict(self) -> dict:
        return asdict(self)


def hash_prompt_template(prompt_template: str) -> str:
    return hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()


def run_judge(
    judge_fn: JudgeFn,
    *,
    model: str,
    prompt_template: str,
    items: Sequence[str],
    n_repeats: int = 1,
) -> tuple[List[float], JudgeProvenance]:
    """Run an injected judge over ``items`` with full provenance.

    The prompt template must contain ``{item}``. Each item is judged
    ``n_repeats`` times; the per-item score is the mean over repeats and
    all raw scores are kept in the provenance.
    """
    if not model or not model.strip():
        raise ValueError("judge model id must be recorded (got empty)")
    if "{item}" not in prompt_template:
        raise ValueError("prompt_template must contain a {item} placeholder")
    if n_repeats < 1:
        raise ValueError(f"n_repeats must be >= 1, got {n_repeats}")
    raw: List[List[float]] = []
    means: List[float] = []
    for item in items:
        prompt = prompt_template.format(item=item)
        repeats = [float(judge_fn(prompt)) for _ in range(n_repeats)]
        for score in repeats:
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"judge score out of [0, 1]: {score!r}")
        raw.append(repeats)
        means.append(sum(repeats) / len(repeats))
    provenance = JudgeProvenance(
        judge_model=model,
        judge_kind="model",
        prompt_template_hash=hash_prompt_template(prompt_template),
        n_repeats=n_repeats,
        raw_scores=raw,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return means, provenance


def manual_scores(
    scores: Sequence[float],
    *,
    rater: str,
    rubric: str,
) -> tuple[List[float], JudgeProvenance]:
    """Human scoring entry point (human audit family).

    ``rubric`` is the human-readable scoring rubric; its hash is recorded
    exactly like a model judge's prompt template hash.
    """
    if not rater or not rater.strip():
        raise ValueError("human rater id must be recorded (got empty)")
    for score in scores:
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"human score out of [0, 1]: {score!r}")
    provenance = JudgeProvenance(
        judge_model=f"human:{rater}",
        judge_kind="human",
        prompt_template_hash=hash_prompt_template(rubric),
        n_repeats=1,
        raw_scores=[[float(s)] for s in scores],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return [float(s) for s in scores], provenance


def judged_metric(
    name: str,
    judge_fn: Optional[JudgeFn],
    *,
    model: str = "",
    prompt_template: str = "",
    items: Sequence[str] = (),
    n_repeats: int = 1,
    human_review: Optional[dict] = None,
) -> MetricResult:
    """Compute a judge-based metric, or record it as ``not_measured``.

    With ``judge_fn=None`` the result is explicitly ``not_measured`` --
    never a default perfect score.
    """
    if judge_fn is None:
        return MetricResult(
            name=name,
            value=None,
            status="not_measured",
            kind="judge",
            n_cases=len(items),
            reason="no_judge: judge-based metric requires an injected judge "
                   "callable (model or human); refusing to default to a perfect score",
        )
    scores, provenance = run_judge(
        judge_fn,
        model=model,
        prompt_template=prompt_template,
        items=items,
        n_repeats=n_repeats,
    )
    value = sum(scores) / len(scores) if scores else None
    return MetricResult(
        name=name,
        value=value,
        status="measured" if value is not None else "not_measured",
        kind="judge",
        n_cases=len(items),
        reason=None if value is not None else "no_items",
        judge=provenance.to_dict(),
        human_review=human_review,
    )
