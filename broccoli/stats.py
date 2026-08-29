"""Statistics store + query history (SystemDesign.md §5).

Two jobs:

1. Feed the cost model (cardinalities, calibrated curves).
2. Log every {features, plan, estimate, actual} tuple. That log is both the
   observability story AND the training set a LearnedPolicy will need later —
   which is why the loop is wired now even though nothing learns yet.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

MAX_HISTORY = 10_000


@dataclass
class HistoryEntry:
    features: Dict[str, Any]
    plan: str
    est_latency_ms: float
    est_recall: float
    actual_latency_ms: float
    n_results: int

    @property
    def estimate_error(self) -> float:
        return (abs(self.est_latency_ms - self.actual_latency_ms)
                / max(self.actual_latency_ms, 1e-6))


@dataclass
class StatisticsStore:
    n_docs: int = 0
    history: List[HistoryEntry] = field(default_factory=list)
    index_stats: Dict[str, Any] = field(default_factory=dict)

    def observe(self, features: Dict[str, Any], plan_name: str,
                est_latency_ms: float, est_recall: float,
                actual_latency_ms: float, n_results: int) -> None:
        self.history.append(HistoryEntry(
            features=features, plan=plan_name, est_latency_ms=est_latency_ms,
            est_recall=est_recall, actual_latency_ms=actual_latency_ms,
            n_results=n_results))
        if len(self.history) > MAX_HISTORY:
            # ponytail: fixed-size ring by truncation. Ceiling: loses the oldest
            # workload signal. Upgrade path: sampled/aggregated retention.
            del self.history[: len(self.history) - MAX_HISTORY]

    # --------------------------- reporting --------------------------------- #
    def plan_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for h in self.history:
            counts[h.plan] = counts.get(h.plan, 0) + 1
        return counts

    def mean_estimate_error(self) -> float:
        """A large value means the cost model is confidently wrong — treated as
        a first-class defect (Approach.md §7)."""
        if not self.history:
            return 0.0
        return sum(h.estimate_error for h in self.history) / len(self.history)

    def summary(self) -> Dict[str, Any]:
        return {
            "n_docs": self.n_docs,
            "queries": len(self.history),
            "plans": self.plan_counts(),
            "mean_estimate_error": round(self.mean_estimate_error(), 4),
            "indexes": self.index_stats,
        }

    # --------------------------- persistence ------------------------------- #
    def save(self, path: str) -> None:
        payload = {
            "n_docs": self.n_docs,
            "index_stats": self.index_stats,
            "history": [asdict(h) for h in self.history[-1000:]],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    @classmethod
    def load(cls, path: str) -> "StatisticsStore":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        store = cls(n_docs=payload.get("n_docs", 0),
                    index_stats=payload.get("index_stats", {}))
        store.history = [HistoryEntry(**h) for h in payload.get("history", [])]
        return store
