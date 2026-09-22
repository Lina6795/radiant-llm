"""Per-partition context budget model.

The upstream ``radiant_llm.py`` tracks a single undifferentiated
``context_usage_percent`` with hardcoded 70%/85% thresholds. This module
replaces that with an explicit, configurable partition model: each
context region has its own token quota, margins are computed per
partition, and over-allocation is a first-class, checkable condition.

Default layout targets a 128k-token window:

    system         8,192   prompts, skill manifests, tool schemas
    active_turn   16,384   the current user turn
    memory        24,576   conversation memory / session summaries
    evidence      49,152   retrieved evidence (M4 candidates)
    artifact      12,288   artifact pointers / small inline artifacts
    tool_result   12,288   tool outputs (after isolation stubs)
    --------------------
    quotas       122,880
    response      8,192   reserve for the model's own reply
    total        131,072   (128 * 1024)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

PARTITIONS = ("system", "active_turn", "memory", "evidence",
              "artifact", "tool_result")

# Partitions the compressor is allowed to rewrite. Evidence is absent on
# purpose: evidence/citation text may only be kept whole or dropped whole
# with a reason (semantic red line, see docs/CONTEXT_BUDGET.md).
COMPRESSIBLE_PARTITIONS = ("memory", "artifact", "tool_result")

DEFAULT_TOTAL_TOKENS = 128 * 1024

DEFAULT_QUOTAS: Dict[str, int] = {
    "system": 8 * 1024,
    "active_turn": 16 * 1024,
    "memory": 24 * 1024,
    "evidence": 48 * 1024,
    "artifact": 12 * 1024,
    "tool_result": 12 * 1024,
}
DEFAULT_RESPONSE_RESERVE = 8 * 1024


@dataclass
class BudgetConfig:
    total_tokens: int = DEFAULT_TOTAL_TOKENS
    quotas: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_QUOTAS))
    response_reserve: int = DEFAULT_RESPONSE_RESERVE

    def __post_init__(self) -> None:
        unknown = set(self.quotas) - set(PARTITIONS)
        if unknown:
            raise ValueError(f"unknown budget partitions: {sorted(unknown)}")
        for name in PARTITIONS:
            self.quotas.setdefault(name, 0)
        self.validate()

    @property
    def usable_tokens(self) -> int:
        """Tokens available to context content (total minus reply reserve)."""
        return self.total_tokens - self.response_reserve

    def validate(self) -> None:
        if self.total_tokens <= 0:
            raise ValueError("total_tokens must be positive")
        if self.response_reserve < 0:
            raise ValueError("response_reserve must be >= 0")
        allocated = sum(self.quotas.values())
        if allocated > self.usable_tokens:
            raise ValueError(
                f"partition quotas sum to {allocated} tokens but only "
                f"{self.usable_tokens} are usable "
                f"(total={self.total_tokens}, reserve={self.response_reserve})")

    @classmethod
    def scale(cls, total_tokens: int,
              quotas: Optional[Dict[str, int]] = None,
              response_reserve: Optional[int] = None) -> "BudgetConfig":
        """Derive a config for a non-default window size.

        Without explicit quotas the default partition *shares* are scaled
        proportionally to the new usable window.
        """
        reserve = (response_reserve if response_reserve is not None
                   else max(1024, total_tokens // 16))
        if quotas is None:
            usable_default = (DEFAULT_TOTAL_TOKENS - DEFAULT_RESPONSE_RESERVE)
            usable = total_tokens - reserve
            quotas = {name: int(usable * q / usable_default)
                      for name, q in DEFAULT_QUOTAS.items()}
        return cls(total_tokens=total_tokens, quotas=quotas,
                   response_reserve=reserve)


@dataclass
class BudgetUsage:
    """Observed token usage against a :class:`BudgetConfig`."""

    config: BudgetConfig
    used: Dict[str, int] = field(default_factory=dict)

    def set(self, partition: str, tokens: int) -> None:
        if partition not in PARTITIONS:
            raise ValueError(f"unknown partition: {partition!r}")
        self.used[partition] = max(0, int(tokens))

    def get(self, partition: str) -> int:
        return self.used.get(partition, 0)

    def margin(self, partition: str) -> int:
        """Remaining tokens for one partition (negative when over)."""
        return self.config.quotas.get(partition, 0) - self.get(partition)

    def is_over(self, partition: str) -> bool:
        return self.get(partition) > self.config.quotas.get(partition, 0)

    @property
    def total_used(self) -> int:
        return sum(self.used.values())

    @property
    def overall_margin(self) -> int:
        return self.config.usable_tokens - self.total_used

    @property
    def is_over_any_partition(self) -> bool:
        return any(self.is_over(p) for p in PARTITIONS)

    @property
    def is_over_total(self) -> bool:
        return self.total_used > self.config.usable_tokens

    def over_partitions(self) -> Dict[str, int]:
        """{partition: tokens_over_quota} for every over-quota partition."""
        return {p: -self.margin(p) for p in PARTITIONS if self.is_over(p)}

    def to_dict(self) -> Dict[str, int]:
        out = {p: self.get(p) for p in PARTITIONS}
        out["total_used"] = self.total_used
        out["usable"] = self.config.usable_tokens
        return out
