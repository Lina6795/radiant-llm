"""M5: context budget and anchor preservation.

Public surface of the context-budget library. ``radiant_llm.py`` is
deliberately NOT wired to this yet — the controller integration happens
in a later milestone.
"""

from .budgets import (COMPRESSIBLE_PARTITIONS, DEFAULT_QUOTAS, PARTITIONS,
                      BudgetConfig, BudgetUsage)
from .compressor import (CompressionLineage, CompressionResult, Compressor,
                         EvidenceCompressionError)
from .engine import (DECISION_ABSTAIN, DECISION_ASSEMBLE, DECISION_CLARIFY,
                     DECISION_COMPRESS, DECISION_RETRIEVE_MORE,
                     AssembledContext, ContextDecision, ContextEngine,
                     DropRecord, EngineOptions)
from .isolate import ArtifactPointer, ArtifactStore
from .selector import (EvidenceItem, SelectionResult, composite_scores,
                       select_evidence)
from .tokenizer import (BACKEND_HEURISTIC, BACKEND_TIKTOKEN, TokenCounter,
                        count_tokens, default_counter)

__all__ = [
    "ArtifactPointer", "ArtifactStore", "AssembledContext",
    "BACKEND_HEURISTIC", "BACKEND_TIKTOKEN", "BudgetConfig", "BudgetUsage",
    "COMPRESSIBLE_PARTITIONS", "CompressionLineage", "CompressionResult",
    "Compressor", "ContextDecision", "ContextEngine", "DEFAULT_QUOTAS",
    "DECISION_ABSTAIN", "DECISION_ASSEMBLE", "DECISION_CLARIFY",
    "DECISION_COMPRESS", "DECISION_RETRIEVE_MORE", "DropRecord",
    "EngineOptions", "EvidenceCompressionError", "EvidenceItem",
    "PARTITIONS", "SelectionResult", "TokenCounter", "composite_scores",
    "count_tokens", "default_counter", "select_evidence",
]
