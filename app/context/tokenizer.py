"""Unified token counting for the context-budget subsystem.

Every component in ``app/context`` counts tokens through this module so
budgets, selection, compression and the decision record all speak the
same unit. Backend preference:

1. tiktoken ``o200k_base`` (GPT-4o-family approximation of the deployed
   model's tokenizer) when the package is installed.
2. A calibrated word+char heuristic otherwise.

Heuristic formula and calibration
---------------------------------
``tokens ~= 0.8 * words + 0.07 * chars``. The coefficients were fitted
by hand against o200k counts on four reference texts (measured
2026-09-22 with tiktoken 0.x):

===========================  ======  ============  ==========
text class                   error   example       note
===========================  ======  ============  ==========
English technical prose      +14%    paper abstract
casual chat                   -9%    user turns
LaTeX-heavy corpus (M0)      -28%    Nougat output  math markup
                                                        tokenizes heavily
dense code                   -52%    Python         under-counts badly
===========================  ======  ============  ==========

The dominant error source is token density variance across text classes:
o200k spans 3.9-6.2 chars/token on prose/chat but ~2.4 on code, and no
single linear estimator closes that gap. This is still strictly better
than the upstream ``chars // 4`` shortcut (no calibration, no word
signal, -36% on the M0 corpus). When the fallback is active,
``TokenCounter.is_approximate`` is True and callers should treat all
absolute budgets as approximate — install tiktoken for exact counts.
"""

from __future__ import annotations

import math
import os
import re
from typing import Iterable, Optional

BACKEND_TIKTOKEN = "tiktoken-o200k"
BACKEND_HEURISTIC = "heuristic-calibrated"

HEURISTIC_WORD_WEIGHT = 0.8
HEURISTIC_CHAR_WEIGHT = 0.07

_WORD_RE = re.compile(r"\S+")

_tiktoken_encoding = None
_tiktoken_tried = False


def _load_tiktoken():
    global _tiktoken_encoding, _tiktoken_tried
    if _tiktoken_tried:
        return _tiktoken_encoding
    _tiktoken_tried = True
    try:
        import tiktoken  # type: ignore

        _tiktoken_encoding = tiktoken.get_encoding("o200k_base")
    except Exception:
        _tiktoken_encoding = None
    return _tiktoken_encoding


class TokenCounter:
    """One counting interface for the whole library.

    ``backend`` may be ``"auto"`` (default), ``"tiktoken"`` (fail if the
    package is missing) or ``"heuristic"`` (force the fallback, used by
    tests to exercise both paths deterministically).
    """

    def __init__(self, backend: str = "auto") -> None:
        if backend == "auto":
            self.backend = (BACKEND_TIKTOKEN if _load_tiktoken() is not None
                            else BACKEND_HEURISTIC)
        elif backend == "tiktoken":
            if _load_tiktoken() is None:
                raise RuntimeError("tiktoken backend requested but not installed")
            self.backend = BACKEND_TIKTOKEN
        elif backend == "heuristic":
            self.backend = BACKEND_HEURISTIC
        else:
            raise ValueError(f"unknown tokenizer backend: {backend!r}")

    # ------------------------------------------------------------------
    @property
    def is_approximate(self) -> bool:
        return self.backend == BACKEND_HEURISTIC

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self.backend == BACKEND_TIKTOKEN:
            return len(_tiktoken_encoding.encode(text))
        words = len(_WORD_RE.findall(text))
        return max(1, math.ceil(words * HEURISTIC_WORD_WEIGHT
                                + len(text) * HEURISTIC_CHAR_WEIGHT))

    def count_many(self, texts: Iterable[str]) -> int:
        return sum(self.count(t) for t in texts)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Approximate character-level cut at a token boundary.

        Used ONLY by the rule compressor on non-evidence partitions;
        evidence text is never passed through this.
        """
        if self.count(text) <= max_tokens:
            return text
        if self.backend == BACKEND_TIKTOKEN:
            ids = _tiktoken_encoding.encode(text)[:max_tokens]
            return _tiktoken_encoding.decode(ids)
        # Heuristic: binary-search the char prefix that fits.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]


_default_counter: Optional[TokenCounter] = None


def default_counter() -> TokenCounter:
    """Process-wide default counter (auto backend)."""
    global _default_counter
    if _default_counter is None:
        _default_counter = TokenCounter(
            os.getenv("RADIANT_TOKENIZER_BACKEND", "auto"))
    return _default_counter


def count_tokens(text: str, counter: Optional[TokenCounter] = None) -> int:
    return (counter or default_counter()).count(text)
