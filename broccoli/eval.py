"""Evaluation harness (Approach.md §7, Research.md §5).

The rule this module enforces: a latency number without a quality number is
meaningless. Every strategy is reported as (recall, nDCG, latency) together,
and the headline metric is latency-at-fixed-recall.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# ------------------------------- metrics ----------------------------------- #


def recall_at_k(retrieved: Sequence[str], relevant: set, k: int) -> float:
    if not relevant:
        return 1.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: set, k: int) -> float:
    if k <= 0:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / k


def mrr(retrieved: Sequence[str], relevant: set) -> float:
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevance: Dict[str, float], k: int) -> float:
    """Graded relevance with position discounting."""
    gains = [relevance.get(doc_id, 0.0) for doc_id in retrieved[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return (dcg / idcg) if idcg > 0 else 0.0


# ------------------------------- harness ----------------------------------- #


@dataclass
class Judgment:
    """One evaluation query and its ground truth."""

    query: Dict[str, Any]                       # kwargs for Index.search
    relevant: Dict[str, float] = field(default_factory=dict)  # doc id -> grade

    @property
    def relevant_set(self) -> set:
        return {d for d, g in self.relevant.items() if g > 0}


@dataclass
class StrategyReport:
    name: str
    recall: float
    ndcg: float
    mrr: float
    latency_p50: float
    latency_p95: float
    meets_target: bool
    work: float = 0.0
    """Mean work units (postings scanned + distance comparisons).

    Wall-clock on a Python reference implementation is noisy enough to invert
    the ranking of two plans that differ by a fraction of a millisecond. Work
    units are deterministic, so they are the primary cost metric here and
    latency is corroborating evidence.
    """

    def row(self) -> str:
        flag = "yes" if self.meets_target else "NO"
        return (f"{self.name:<22}{self.recall:>9.3f}{self.ndcg:>9.3f}"
                f"{self.mrr:>8.3f}{self.work:>12.0f}{self.latency_p50:>10.2f}"
                f"{self.latency_p95:>10.2f}{flag:>9}")


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


class Harness:
    """Runs judged queries through one or more strategies and compares them.

    A 'strategy' is just a `pin` value (or None to let the optimizer choose),
    which is exactly how we prove the optimizer beats every fixed strategy.
    """

    def __init__(self, index, judgments: Sequence[Judgment], k: int = 10,
                 recall_target: float = 0.9):
        self.index = index
        self.judgments = list(judgments)
        self.k = k
        self.recall_target = recall_target

    def _warmup(self, pin: Optional[str], n: int = 40) -> None:
        """Untimed passes before measuring.

        Without this, whichever strategy runs first absorbs all the one-off
        cost (ANN graph paging, numpy buffer allocation, import side effects)
        and looks slower than it is. That artifact is big enough to invert the
        ranking, so it would silently corrupt every latency comparison.
        """
        for j in self.judgments[:n]:
            kwargs = dict(j.query)
            kwargs.setdefault("k", self.k)
            if pin is not None:
                kwargs["pin"] = pin
            try:
                self.index.search(**kwargs)
            except ValueError:
                return

    def run_strategy(self, name: str, pin: Optional[str] = None) -> StrategyReport:
        self._warmup(pin)
        recalls, ndcgs, mrrs, latencies, works = [], [], [], [], []
        for j in self.judgments:
            kwargs = dict(j.query)
            kwargs.setdefault("k", self.k)
            kwargs["explain"] = True          # needed for deterministic work units
            if pin is not None:
                kwargs["pin"] = pin
            started = time.perf_counter()
            try:
                results = self.index.search(**kwargs)
            except ValueError:
                # A fixed strategy may be inapplicable to a query (e.g. vector-only
                # on a query with no semantic intent). That is a real failure of
                # that strategy, scored as zero rather than hidden.
                recalls.append(0.0); ndcgs.append(0.0); mrrs.append(0.0)
                latencies.append((time.perf_counter() - started) * 1000.0)
                works.append(0.0)
                continue
            latencies.append((time.perf_counter() - started) * 1000.0)
            works.append(sum(s.examined for s in results.explain.stages))
            ids = results.ids
            recalls.append(recall_at_k(ids, j.relevant_set, self.k))
            ndcgs.append(ndcg_at_k(ids, j.relevant, self.k))
            mrrs.append(mrr(ids, j.relevant_set))

        n = max(len(recalls), 1)
        mean_recall = sum(recalls) / n
        return StrategyReport(
            name=name,
            recall=mean_recall,
            ndcg=sum(ndcgs) / n,
            mrr=sum(mrrs) / n,
            latency_p50=_percentile(latencies, 50),
            latency_p95=_percentile(latencies, 95),
            meets_target=mean_recall >= self.recall_target,
            work=sum(works) / n,
        )

    def compare(self, strategies: Optional[Dict[str, Optional[str]]] = None
                ) -> List[StrategyReport]:
        """Default comparison: the optimizer vs. every fixed strategy."""
        if strategies is None:
            strategies = {"ADAPTIVE (optimizer)": None, "lexical": "lexical",
                          "vector": "vector", "hybrid_rrf": "hybrid_rrf"}
        return [self.run_strategy(name, pin) for name, pin in strategies.items()]

    def report(self, strategies: Optional[Dict[str, Optional[str]]] = None,
               reports: Optional[Sequence[StrategyReport]] = None) -> str:
        """Pass `reports` from a previous `compare()` to avoid re-running the
        whole workload (which would also double-count the query history)."""
        reports = list(reports) if reports is not None else self.compare(strategies)
        header = (f"{'strategy':<22}{'recall':>9}{'nDCG':>9}{'MRR':>8}"
                  f"{'work':>12}{'p50 ms':>10}{'p95 ms':>10}{'target':>9}")
        lines = [f"\n{len(self.judgments)} judged queries, k={self.k}, "
                 f"recall target={self.recall_target}\n", header, "-" * len(header)]
        lines += [r.row() for r in reports]
        lines.append("-" * len(header))

        adaptive = next((r for r in reports if r.name.startswith("ADAPTIVE")), None)
        fixed = [r for r in reports if not r.name.startswith("ADAPTIVE")]
        meeting = [r for r in fixed if r.meets_target]
        if adaptive and meeting:
            best = min(meeting, key=lambda r: r.work)
            ratio = best.work / max(adaptive.work, 1e-9)
            lines.append(f"\nwork-at-fixed-recall: adaptive {adaptive.work:.0f} units "
                         f"vs best fixed ({best.name}) {best.work:.0f} → {ratio:.2f}x")
            best_lat = min(meeting, key=lambda r: r.latency_p95)
            speedup = best_lat.latency_p95 / max(adaptive.latency_p95, 1e-9)
            lines.append(f"latency-at-fixed-recall: adaptive p95 "
                         f"{adaptive.latency_p95:.2f}ms vs best fixed "
                         f"({best_lat.name}) {best_lat.latency_p95:.2f}ms "
                         f"-> {speedup:.2f}x")
        return "\n".join(lines) + "\n"


def latency_at_fixed_recall(reports: Sequence[StrategyReport],
                            target: float) -> Optional[StrategyReport]:
    """The north-star metric: cheapest strategy that actually hits the recall bar."""
    meeting = [r for r in reports if r.recall >= target]
    return min(meeting, key=lambda r: r.latency_p95) if meeting else None


def work_at_fixed_recall(reports: Sequence[StrategyReport],
                         target: float) -> Optional[StrategyReport]:
    """Same idea, measured in deterministic work units instead of wall-clock."""
    meeting = [r for r in reports if r.recall >= target]
    return min(meeting, key=lambda r: r.work) if meeting else None
