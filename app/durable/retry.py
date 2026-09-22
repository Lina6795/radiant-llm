"""RADIANT-Control M3: classified retry with an injectable clock.

Only ``ToolResult.status == retryable_error`` is ever retried.
``terminal_error`` and ``denied`` are never retried -- retrying a denial
would be a policy bypass, and retrying a terminal error is waste. The clock
(``now`` + ``sleep``) is injected so backoff is fully deterministic offline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from app.control.models import ToolResult, ToolStatus


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    reason: str
    delay_ms: float = 0.0


@dataclass
class ExponentialBackoff:
    max_attempts: int = 3
    base_delay_ms: float = 100.0
    multiplier: float = 2.0
    max_delay_ms: float = 5_000.0
    clock: Clock = field(default_factory=RealClock)

    def classify(self, result: ToolResult) -> str:
        """Error classification: 'success' | 'retryable' | 'terminal'."""
        if result.status == ToolStatus.SUCCESS:
            return "success"
        if result.status == ToolStatus.RETRYABLE_ERROR:
            return "retryable"
        return "terminal"  # terminal_error and denied are both final

    def delay_for(self, attempt: int) -> float:
        """Backoff delay in ms before retry attempt ``attempt + 1``."""
        return min(self.base_delay_ms * (self.multiplier ** max(attempt - 1, 0)), self.max_delay_ms)

    def decide(self, result: ToolResult, attempt: int, *, policy_allows: bool) -> RetryDecision:
        classification = self.classify(result)
        if classification == "success":
            return RetryDecision(retry=False, reason="success")
        if classification == "terminal":
            return RetryDecision(retry=False, reason=f"terminal:{result.status.value}")
        if not policy_allows:
            return RetryDecision(retry=False, reason="retry_policy_disabled")
        if attempt >= self.max_attempts:
            return RetryDecision(retry=False, reason=f"attempts_exhausted:{attempt}")
        return RetryDecision(retry=True, reason="transient", delay_ms=self.delay_for(attempt))

    def wait(self, attempt: int) -> None:
        self.clock.sleep(self.delay_for(attempt) / 1000.0)
