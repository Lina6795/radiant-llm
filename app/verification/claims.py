"""RADIANT-Control M7: atomic claim extraction.

Rule-based splitter (deterministic, offline) with an injectable LLM
callable for environments where a conversational model is available. The
LLM path is validated and falls back to the rule splitter on any error,
so a broken model can never produce silently malformed claims.

Claim types: numeric / unit / factual / relational / visual.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    NUMERIC = "numeric"
    UNIT = "unit"
    FACTUAL = "factual"
    RELATIONAL = "relational"
    VISUAL = "visual"


class Claim(BaseModel):
    claim_id: str
    text: str
    claim_type: ClaimType
    sentence_index: int
    numbers: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)


class SplitResult(BaseModel):
    claims: list[Claim]
    splitter: str  # "rule" | "llm" | "llm_fallback"
    detail: str = ""


# ---------------------------------------------------------------------------
# Extraction primitives
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+|[；;]\s*")
_CITATION_MARKER_RE = re.compile(r"\[(?:ev|mem|doc)[-:][^\]]+\]", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_ENTITY_RE = re.compile(r"\b(?:[A-Z][a-zA-Z]+(?:[- ][A-Z][A-Za-z]+){0,2}|[a-z]+_[a-z]+)\b")

# Units observed in the frozen corpus plus common ML units. Matching is
# case-sensitive for symbols (ms, GB) and case-insensitive for words.
_UNIT_TOKENS = (
    "BLEU", "steps", "step", "layers", "layer", "heads", "head",
    "dimensions", "dimension", "parameters", "epochs", "epoch",
    "warmup", "warm-up", "tokens", "ms", "seconds", "hours", "GB",
    "%", "percent", "perplexity", "PPL", "FLOPs",
)

_RELATIONAL_MARKERS = (
    "consist", "differ", "compared", "versus", " vs ", "more than",
    "less than", "higher", "lower", "left", "right", "above", "below",
    "outperform", "between", "respectively",
)

_VISUAL_MARKERS = ("figure", "diagram", "layout", "heatmap", "plot", "chart", "table")

_STOPWORDS = frozenset(
    "the a an and or of to in on for with by is are was were be been it its "
    "this that these those as at from into over under each per than then so".split()
)


def make_claim_id(text: str) -> str:
    norm = " ".join(text.lower().split())
    return "cl-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def extract_numbers(text: str) -> list[str]:
    return _NUMBER_RE.findall(text or "")


def normalize_number(token: str) -> Optional[float]:
    """'100,000' -> 100000.0; returns None when not parseable."""
    try:
        return float(token.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def numbers_match(a: str, b: str, *, rel_tol: float = 1e-6) -> bool:
    fa, fb = normalize_number(a), normalize_number(b)
    if fa is None or fb is None:
        return a.strip() == b.strip()
    if fa == fb:
        return True
    return abs(fa - fb) <= rel_tol * max(abs(fa), abs(fb))


def extract_units(text: str) -> list[str]:
    found: list[str] = []
    for tok in _UNIT_TOKENS:
        if tok.isupper() or tok in ("%",):
            if tok in text:
                found.append(tok)
        elif re.search(rf"\b{re.escape(tok)}\b", text, flags=re.IGNORECASE):
            found.append(tok)
    return found


def extract_entities(text: str) -> list[str]:
    ents = [m.group(0) for m in _ENTITY_RE.finditer(text or "")]
    return [e for e in ents if e.lower() not in _STOPWORDS and len(e) > 1]


def content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower()) if t not in _STOPWORDS}


def classify_claim(sentence: str, numbers: list[str], units: list[str]) -> ClaimType:
    low = sentence.lower()
    if any(m in low for m in _VISUAL_MARKERS):
        return ClaimType.VISUAL
    if numbers and units:
        return ClaimType.UNIT
    if numbers:
        return ClaimType.NUMERIC
    if any(m in low for m in _RELATIONAL_MARKERS):
        return ClaimType.RELATIONAL
    return ClaimType.FACTUAL


def split_sentences(answer: str) -> list[str]:
    # Mask bracketed citation spans before splitting: a compound citation
    # like "[ev-a, page 2; ev-b, page 1]" contains '; ' and must not be cut
    # mid-bracket (fragments leak citation digits into number extraction).
    masked: list[str] = []
    def _mask(m):
        masked.append(m.group(0))
        return f"\x00{len(masked) - 1}\x00"

    work = re.sub(r"\[[^\]]*\]", _mask, answer or "")
    parts = []
    for raw in _SENTENCE_SPLIT_RE.split(work):
        restored = re.sub(r"\x00(\d+)\x00", lambda m: masked[int(m.group(1))], raw)
        s = restored.strip().lstrip("-*•0123456789.) ").strip()
        if len(s) >= 3:
            parts.append(s)
    return parts


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------

def _rule_split(answer: str) -> list[Claim]:
    claims: list[Claim] = []
    for idx, sentence in enumerate(split_sentences(answer)):
        # Strip citation markers like [ev-1467...] before numeric analysis:
        # the digits inside an evidence citation are NOT claim content and
        # would otherwise misclassify every cited claim as NUMERIC.
        analysis_text = _CITATION_MARKER_RE.sub("", sentence)
        numbers = extract_numbers(analysis_text)
        units = extract_units(analysis_text)
        claims.append(
            Claim(
                claim_id=make_claim_id(sentence),
                text=sentence,
                claim_type=classify_claim(analysis_text, numbers, units),
                sentence_index=idx,
                numbers=numbers,
                units=units,
                entities=extract_entities(analysis_text),
            )
        )
    return claims


_LLM_INSTRUCTIONS = (
    "Split the answer below into atomic factual claims. Return a JSON list, "
    "each item {\"text\": str, \"claim_type\": one of "
    "[numeric, unit, factual, relational, visual]}. JSON only, no prose."
)


def _claims_from_llm_payload(payload: Any) -> list[Claim]:
    if not isinstance(payload, list):
        raise ValueError("LLM splitter must return a JSON list")
    claims: list[Claim] = []
    for idx, item in enumerate(payload):
        text = str(item["text"]).strip()
        ctype = ClaimType(str(item.get("claim_type", "factual")))
        numbers = extract_numbers(text)
        units = extract_units(text)
        claims.append(
            Claim(
                claim_id=make_claim_id(text),
                text=text,
                claim_type=ctype,
                sentence_index=idx,
                numbers=numbers,
                units=units,
                entities=extract_entities(text),
            )
        )
    return claims


def split_claims(
    answer: str,
    llm_callable: Optional[Callable[[str], Any]] = None,
) -> SplitResult:
    """Split an answer into atomic claims.

    ``llm_callable`` receives (instructions + answer) and must return a JSON
    string or parsed list of {"text","claim_type"}. Any failure falls back to
    the deterministic rule splitter and says so in ``splitter``.
    """
    if not (answer or "").strip():
        return SplitResult(claims=[], splitter="rule", detail="empty answer")

    if llm_callable is not None:
        try:
            raw = llm_callable(f"{_LLM_INSTRUCTIONS}\n\nANSWER:\n{answer}")
            payload = json.loads(raw) if isinstance(raw, str) else raw
            claims = _claims_from_llm_payload(payload)
            if claims:
                return SplitResult(claims=claims, splitter="llm")
            raise ValueError("LLM splitter returned zero claims")
        except Exception as exc:  # deterministic fallback, loudly labelled
            return SplitResult(
                claims=_rule_split(answer),
                splitter="llm_fallback",
                detail=f"{type(exc).__name__}: {exc}",
            )
    return SplitResult(claims=_rule_split(answer), splitter="rule")
