"""M4 retrieval & evidence-control package.

Hybrid recall (BM25 + dense) -> RRF fusion -> metadata filters ->
optional rerank -> relevance/anchor gates -> sufficiency grader, with a
structured per-stage trace for every query.
"""

from .types import Candidate
from .pipeline import RetrievalConfig, RetrievalPipeline

__all__ = ["Candidate", "RetrievalConfig", "RetrievalPipeline"]
