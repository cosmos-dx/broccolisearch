"""Query (intent), filter predicates, and the Plan the optimizer emits.

Design rule from PRD.md §8: the caller states INTENT and CONSTRAINTS; the
engine owns STRATEGY. Nothing in `Query` names an index or an algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ----------------------------- predicates ---------------------------------- #


class Predicate:
    """A structured constraint on one field."""

    def describe(self) -> str:  # pragma: no cover - trivial
        return self.__class__.__name__.lower()


@dataclass
class Eq(Predicate):
    value: Any

    def describe(self) -> str:
        return f"= {self.value!r}"


@dataclass
class OneOf(Predicate):
    values: Sequence[Any]

    def describe(self) -> str:
        return f"in {list(self.values)!r}"


@dataclass
class Range(Predicate):
    """Half-open-ish numeric range; None means unbounded on that side."""

    low: Optional[float] = None
    high: Optional[float] = None
    include_low: bool = True
    include_high: bool = True

    def describe(self) -> str:
        lo = "-inf" if self.low is None else self.low
        hi = "+inf" if self.high is None else self.high
        return f"in [{lo}, {hi}]"


# Sugar so callers write `where={"price": lt(10)}` (PRD.md §8).
def lt(v: float) -> Range:
    return Range(high=v, include_high=False)


def lte(v: float) -> Range:
    return Range(high=v)


def gt(v: float) -> Range:
    return Range(low=v, include_low=False)


def gte(v: float) -> Range:
    return Range(low=v)


def between(low: float, high: float) -> Range:
    return Range(low=low, high=high)


def one_of(*values: Any) -> OneOf:
    if len(values) == 1 and isinstance(values[0], (list, tuple, set)):
        values = tuple(values[0])
    return OneOf(list(values))


def normalize_where(where: Optional[Dict[str, Any]]) -> Dict[str, Predicate]:
    """Bare values become Eq; lists become OneOf."""
    if not where:
        return {}
    out: Dict[str, Predicate] = {}
    for name, value in where.items():
        if isinstance(value, Predicate):
            out[name] = value
        elif isinstance(value, (list, tuple, set)):
            out[name] = OneOf(list(value))
        else:
            out[name] = Eq(value)
    return out


# ----------------------------- time ---------------------------------------- #

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> float:
    """'30d' -> 2592000.0 seconds."""
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"cannot parse duration '{text}' (try '30d', '12h', '15m')")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


# ----------------------------- query --------------------------------------- #


@dataclass
class Query:
    """What the caller wants, never how to get it."""

    text: Optional[str] = None
    semantic: Optional[List[float]] = None
    where: Dict[str, Predicate] = field(default_factory=dict)
    recent: Optional[str] = None          # hard time window, e.g. "30d"
    decay: Optional[str] = None           # recency half-life, e.g. "7d"
    time_field: Optional[str] = None
    k: int = 10
    recall_target: float = 0.9
    explain: bool = False
    pin: Optional[str] = None             # force a strategy (debug/benchmark)
    vector_field: Optional[str] = None
    text_fields: Optional[List[str]] = None

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())

    @property
    def has_semantic(self) -> bool:
        return self.semantic is not None

    @property
    def has_filter(self) -> bool:
        return bool(self.where) or self.recent is not None


# ----------------------------- plan ---------------------------------------- #


@dataclass
class PlanStep:
    op: str                                # filter | lexical | vector | fuse | rank
    budget: Optional[Any] = None           # types.Budget for retrieval ops
    detail: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        bits = []
        if self.budget is not None:
            if getattr(self.budget, "domain", None) is not None:
                bits.append(f"domain={len(self.budget.domain)}")
            if self.op == "vector":
                bits.append(f"ef={self.budget.ef}")
            bits.append(f"n={self.budget.candidates}")
        bits += [f"{k}={v}" for k, v in self.detail.items()]
        return f"{self.op}({', '.join(str(b) for b in bits)})"


@dataclass
class PlanEstimate:
    latency_ms: float = 0.0
    recall: float = 1.0


@dataclass
class StageStat:
    op: str
    candidates_in: int
    candidates_out: int
    latency_ms: float
    examined: int = 0


@dataclass
class Plan:
    """What the optimizer emits and the executor runs."""

    name: str
    steps: List[PlanStep] = field(default_factory=list)
    fusion: str = "rrf"                    # rrf | weighted | none
    ranker: str = "fusion"                 # fusion | bm25 | vector
    estimate: PlanEstimate = field(default_factory=PlanEstimate)

    def describe(self) -> str:
        chain = " -> ".join(str(s) for s in self.steps)
        return f"{self.name}: {chain} | fusion={self.fusion} ranker={self.ranker}"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.describe()


@dataclass
class Explain:
    """Estimated vs. actual — the optimizer's report card (SystemDesign §6.6)."""

    plan: Plan
    stages: List[StageStat] = field(default_factory=list)
    actual_latency_ms: float = 0.0   # end to end, what the caller waited for
    execution_ms: float = 0.0        # the plan only, excluding planning/marshalling
    considered: List[str] = field(default_factory=list)

    @property
    def estimate_error(self) -> float:
        est = self.plan.estimate.latency_ms
        return abs(est - self.actual_latency_ms) / max(self.actual_latency_ms, 1e-6)

    def describe(self) -> str:
        lines = [self.plan.describe(),
                 f"  estimated: {self.plan.estimate.latency_ms:.2f}ms "
                 f"recall~{self.plan.estimate.recall:.2f}",
                 f"  actual:    {self.actual_latency_ms:.2f}ms "
                 f"({self.execution_ms:.2f}ms executing)"]
        for s in self.stages:
            lines.append(f"  - {s.op:<8} in={s.candidates_in:<6} out={s.candidates_out:<6}"
                         f" examined={s.examined:<7} {s.latency_ms:.2f}ms")
        if self.considered:
            lines.append(f"  considered: {', '.join(self.considered)}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.describe()


@dataclass
class Hit:
    id: str
    score: float
    doc: Dict[str, Any] = field(default_factory=dict)


class Results(list):
    """A list of Hits that also carries the plan/explain payload."""

    def __init__(self, hits: List[Hit], explain: Optional[Explain] = None):
        super().__init__(hits)
        self.explain = explain

    @property
    def plan(self) -> Optional[Plan]:
        return self.explain.plan if self.explain else None

    @property
    def ids(self) -> List[str]:
        return [h.id for h in self]
