"""Index engines behind one interface (Architecture.md §4.1).

The optimizer only ever sees `BaseIndex`. That is what lets it plan across
lexical, vector and structured axes uniformly — and what lets a graph or
temporal axis be added later without touching the optimizer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from ..types import Budget, Capabilities, CandidateSet, CostEstimate


class BaseIndex(ABC):
    """A source of candidate documents and/or scores for a query."""

    name: str = "index"

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Which query capabilities this index can serve."""

    @abstractmethod
    def estimate(self, subquery: Any, budget: Budget) -> CostEstimate:
        """Cheap (latency, recall, cardinality) estimate WITHOUT executing."""

    @abstractmethod
    def search(self, subquery: Any, budget: Budget) -> CandidateSet:
        """Produce candidates under a budget."""

    def statistics(self) -> Dict[str, Any]:
        return {}


from .lexical import LexicalIndex        # noqa: E402  (public re-export)
from .structured import StructuredIndex  # noqa: E402
from .vector import VectorIndex          # noqa: E402

__all__ = ["BaseIndex", "LexicalIndex", "StructuredIndex", "VectorIndex",
           "Budget", "Capabilities", "CandidateSet", "CostEstimate"]
