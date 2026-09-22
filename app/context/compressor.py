"""Two-level compression for compressible context partitions.

Level 1 (deterministic rules, no model): drop boilerplate lines (page
footers, separator runs), collapse redundant whitespace, then — if still
over target — keep the first and last sentences of long segments plus
the highest-value middle sentences (digit / proper-noun density). If
even the first+last skeleton exceeds the target, the rules declare
insufficiency instead of butchering the text.

Level 2 (optional LLM recursion): reached only when level 1 is
insufficient. A summarizer callable is injected
(``llm_summarizer(text, target_tokens) -> str``); in tests this is a
stub. The text is split into segments, each segment summarized, and the
joined summary recursively re-summarized up to ``max_llm_depth`` times.
A token-level hard truncate exists only as the last-resort fallback when
no summarizer is wired or the recursion exhausts its depth — it is
always recorded in the lineage steps.

Semantic red line: evidence and citation content is NEVER rewritten by
this module. :meth:`Compressor.compress_partition` raises
``EvidenceCompressionError`` for the ``evidence`` partition — evidence
may only be kept whole or dropped whole (with a reason) by the selector
and engine. Compression applies only to memory / artifact / tool_result
summary regions.

Every compression returns a :class:`CompressionLineage` recording the
input hash, method, model, and token counts before/after, so any
compressed span is traceable back to its pre-compression content hash.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from .budgets import COMPRESSIBLE_PARTITIONS, PARTITIONS
from .tokenizer import TokenCounter, default_counter

SummarizerFn = Callable[[str, int], str]


class EvidenceCompressionError(ValueError):
    """Raised when compression is attempted on the evidence partition."""


_BOILERPLATE_RE = re.compile(
    r"^\s*("
    r"page\s+\d+(\s+of\s+\d+)?"
    r"|[-=_*~]{3,}"
    r"|\d{1,4}"
    r")\s*$",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_KEY_TOKEN_RE = re.compile(r"(\d|[A-Z][a-zA-Z]{2,})")

METHOD_RULES = "rules"
METHOD_LLM = "rules+llm_recursive"


@dataclass
class CompressionLineage:
    input_hash: str          # sha256 of the pre-compression text
    output_hash: str         # sha256 of the compressed text
    method: str              # "rules" | "rules+llm_recursive"
    model: Optional[str]     # summarizer model name, None for pure rules
    tokens_before: int
    tokens_after: int
    target_tokens: int
    steps: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompressionResult:
    text: str
    lineage: CompressionLineage


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Compressor:
    def __init__(
        self,
        counter: Optional[TokenCounter] = None,
        llm_summarizer: Optional[SummarizerFn] = None,
        model_name: Optional[str] = None,
        max_llm_depth: int = 3,
    ) -> None:
        self.counter = counter or default_counter()
        self.llm_summarizer = llm_summarizer
        self.model_name = model_name
        self.max_llm_depth = max_llm_depth

    # ------------------------------------------------------------------
    # Level 1: deterministic rules
    # ------------------------------------------------------------------
    def _drop_boilerplate(self, text: str, steps: List[str]) -> str:
        lines = [ln for ln in text.splitlines() if not _BOILERPLATE_RE.match(ln)]
        if len(lines) != len(text.splitlines()):
            steps.append("drop_boilerplate_lines")
        return "\n".join(lines)

    def _collapse_whitespace(self, text: str, steps: List[str]) -> str:
        out = re.sub(r"[ \t]+", " ", text)
        out = re.sub(r"\n{3,}", "\n\n", out)
        if out != text:
            steps.append("collapse_whitespace")
        return out.strip()

    def _sentence_window(self, text: str, target: int,
                         steps: List[str]) -> Optional[str]:
        """Keep first + last sentences, then best middle sentences.

        Returns ``None`` when even the first+last skeleton cannot fit —
        the caller then escalates to the LLM level (or the last-resort
        hard truncate) instead of silently mangling the text.
        """
        sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
        if len(sentences) <= 2:
            return None

        def key(s: str) -> int:
            return len(_KEY_TOKEN_RE.findall(s))

        def fits(idx_set) -> bool:
            body = " ".join(sentences[i] for i in sorted(idx_set))
            return self.counter.count(body) <= target

        chosen = {0, len(sentences) - 1}
        if not fits(chosen):
            return None
        middle = sorted(range(1, len(sentences) - 1),
                        key=lambda i: key(sentences[i]), reverse=True)
        for i in middle:
            trial = chosen | {i}
            if fits(trial):
                chosen = trial
        steps.append("sentence_window")
        return " ".join(sentences[i] for i in sorted(chosen))

    def _rule_compress(self, text: str, target: int,
                       steps: List[str]) -> tuple:
        """Returns ``(text, fits_target)``."""
        out = self._drop_boilerplate(text, steps)
        out = self._collapse_whitespace(out, steps)
        if self.counter.count(out) <= target:
            return out, True
        windowed = self._sentence_window(out, target, steps)
        if windowed is None:
            return out, False
        return windowed, True

    # ------------------------------------------------------------------
    # Level 2: recursive LLM summarization (injected callable)
    # ------------------------------------------------------------------
    def _llm_recursive(self, text: str, target: int,
                       steps: List[str]) -> str:
        assert self.llm_summarizer is not None
        out = text
        depth = 0
        while (self.counter.count(out) > target
               and depth < self.max_llm_depth):
            depth += 1
            tokens = self.counter.count(out)
            n_segments = max(2, math.ceil(tokens / max(1, target)))
            seg_len = math.ceil(len(out) / n_segments)
            segments = [out[i:i + seg_len]
                        for i in range(0, len(out), seg_len)]
            per_seg_target = max(1, target // len(segments))
            out = " ".join(
                self.llm_summarizer(seg, per_seg_target) for seg in segments)
            steps.append(f"llm_summarize_depth_{depth}")
        return out

    # ------------------------------------------------------------------
    def compress(self, text: str, target_tokens: int,
                 allow_llm: bool = True) -> CompressionResult:
        """Two-level compression of a non-evidence text region."""
        before = self.counter.count(text)
        steps: List[str] = []
        out, fits = self._rule_compress(text, target_tokens, steps)
        method = METHOD_RULES
        if not fits and allow_llm and self.llm_summarizer is not None:
            out = self._llm_recursive(out, target_tokens, steps)
            method = METHOD_LLM
            fits = self.counter.count(out) <= target_tokens
        if not fits:
            # Last resort only; always visible in the lineage.
            out = self.counter.truncate_to_tokens(out, target_tokens)
            steps.append("hard_truncate_tokens")
        after = self.counter.count(out)
        lineage = CompressionLineage(
            input_hash=_hash(text),
            output_hash=_hash(out),
            method=method,
            model=self.model_name if method == METHOD_LLM else None,
            tokens_before=before,
            tokens_after=after,
            target_tokens=target_tokens,
            steps=steps,
        )
        return CompressionResult(text=out, lineage=lineage)

    def compress_partition(self, partition: str, text: str,
                           target_tokens: int,
                           allow_llm: bool = True) -> CompressionResult:
        """Partition-guarded compression — enforces the evidence red line."""
        if partition == "evidence":
            raise EvidenceCompressionError(
                "evidence/citation content may only be kept whole or dropped "
                "whole with a reason; semantic compression of evidence is "
                "forbidden (see docs/CONTEXT_BUDGET.md)")
        if partition not in PARTITIONS:
            raise ValueError(f"unknown partition: {partition!r}")
        if partition not in COMPRESSIBLE_PARTITIONS:
            raise ValueError(
                f"partition {partition!r} is not compressible "
                f"(compressible: {COMPRESSIBLE_PARTITIONS})")
        return self.compress(text, target_tokens, allow_llm=allow_llm)
