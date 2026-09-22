"""RADIANT-Control M2: budget caps and per-workspace budget ledger.

Hard caps bound what any plan may declare (enforced by the Schema Guard).
The ledger bounds what a workspace may spend across runs (enforced by the
Policy Engine before execution and by the Scheduler during execution).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BudgetCaps:
    max_tokens: int = 100_000
    max_tool_calls: int = 10
    max_wall_time_ms: int = 120_000


DEFAULT_CAPS = BudgetCaps()

DEFAULT_WORKSPACE_TOKEN_ALLOWANCE = 100_000


@dataclass
class BudgetLedger:
    """In-memory per-workspace token allowance. Fail-closed: an unknown
    workspace gets the default allowance only if it is registered; unregistered
    workspaces report zero remaining budget."""

    allowances: dict[str, int] = field(default_factory=dict)
    spent: dict[str, int] = field(default_factory=dict)

    def remaining(self, workspace: str) -> int:
        if workspace not in self.allowances:
            return 0
        return self.allowances[workspace] - self.spent.get(workspace, 0)

    def record(self, workspace: str, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        self.spent[workspace] = self.spent.get(workspace, 0) + tokens

    @classmethod
    def with_defaults(cls, workspaces: list[str]) -> "BudgetLedger":
        return cls(allowances={w: DEFAULT_WORKSPACE_TOKEN_ALLOWANCE for w in workspaces})
