"""Shared value types every layer speaks (Architecture.md §4.1).

These are deliberately dumb data holders so `broccoli.optimizer` can reason
about any index without importing a concrete engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Set


@dataclass(frozen=True)
class Capabilities:
    """What a given index can serve. The optimizer plans against these."""

    lexical: bool = False
    vector: bool = False
    structured: bool = False


@dataclass
class Budget:
    """The knob the optimizer turns.

    `candidates` caps how many results a stage may EMIT (usually an overfetch
    multiple of k). `k` is how many the caller actually asked for, which is a
    different question and the one that determines whether a stage has enough
    coverage to be useful. `ef` is the ANN recall/latency dial. `domain` is a
    pushed-down set of allowed doc ids (this is how "don't vector-search your
    whole database" is implemented).
    """

    candidates: int = 100
    ef: int = 64
    domain: Optional[Set[int]] = None
    k: int = 10

    def with_domain(self, domain: Optional[Set[int]]) -> "Budget":
        return Budget(candidates=self.candidates, ef=self.ef, domain=domain,
                      k=self.k)


@dataclass
class CostEstimate:
    """Cost is a (latency, recall) PAIR, never a scalar — that is the whole
    reason this project exists (Research.md §3). `cardinality` is how many
    docs the operator is expected to emit."""

    latency_ms: float
    recall: float
    cardinality: int

    def __post_init__(self) -> None:
        self.recall = min(1.0, max(0.0, self.recall))


@dataclass
class CandidateSet:
    """Doc ids + per-index scores + provenance."""

    scores: Dict[int, float] = field(default_factory=dict)
    source: str = ""
    examined: int = 0  # work units actually spent (for explain / cost calibration)

    @property
    def ids(self) -> Set[int]:
        return set(self.scores)

    def top(self, n: int):
        return sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def __len__(self) -> int:
        return len(self.scores)
