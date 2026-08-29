"""Structured index: bitmaps for exact match, sorted columns for ranges.

This is the cheapest index in the system and therefore the optimizer's favourite
first move: a selective filter shrinks the universe before anything expensive
runs (SystemDesign.md §4.3).

ponytail: bitmaps are Python `set`s, not roaring bitmaps (pyroaring isn't
installed). Ceiling: memory grows linearly with cardinality and intersections
aren't SIMD-compressed. Upgrade path: roaring in the Rust port — the interface
(a set of doc ids + O(1) cardinality) is identical.
"""

from __future__ import annotations

import bisect
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..calibration import TIMING_REPEATS, fit_linear
from ..query import Eq, OneOf, Predicate, Range
from ..types import Budget, Capabilities, CandidateSet, CostEstimate
from . import BaseIndex


class StructuredIndex(BaseIndex):
    name = "structured"

    def __init__(self, fields: Dict[str, str], deleted: Optional[Set[int]] = None):
        """`fields` maps field name -> schema kind."""
        self.fields = dict(fields)
        self.deleted = deleted if deleted is not None else set()
        self.bitmaps: Dict[str, Dict[Any, Set[int]]] = {f: {} for f in fields}
        self.values: Dict[str, Dict[int, Any]] = {f: {} for f in fields}
        self._sorted: Dict[str, Tuple[List[Any], List[int]]] = {}
        self._dirty: Set[str] = set()
        self._all: Set[int] = set()
        # Calibrated: latency = base_ms + docs * sec_per_doc.
        self.sec_per_doc = 4e-8
        self.base_ms = 0.005

    # ------------------------------ build ---------------------------------- #
    def add(self, doc_id: int, fields: Dict[str, Any]) -> None:
        self._all.add(doc_id)
        for name in self.fields:
            if name not in fields:
                continue
            value = fields[name]
            self.bitmaps[name].setdefault(value, set()).add(doc_id)
            self.values[name][doc_id] = value
            self._dirty.add(name)

    def remove(self, doc_id: int) -> None:
        self._all.discard(doc_id)
        for name in self.fields:
            value = self.values[name].pop(doc_id, None)
            if value is not None:
                self.bitmaps[name].get(value, set()).discard(doc_id)
                self._dirty.add(name)

    @property
    def n_docs(self) -> int:
        return len(self._all)

    def live(self) -> Set[int]:
        return self._all - self.deleted if self.deleted else set(self._all)

    def _sorted_column(self, name: str) -> Tuple[List[Any], List[int]]:
        if name in self._dirty or name not in self._sorted:
            pairs = sorted(self.values[name].items(), key=lambda kv: kv[1])
            self._sorted[name] = ([v for _, v in pairs], [d for d, _ in pairs])
            self._dirty.discard(name)
        return self._sorted[name]

    # ---------------------------- filtering -------------------------------- #
    def _match(self, name: str, pred: Predicate) -> Set[int]:
        if name not in self.fields:
            raise KeyError(f"'{name}' is not a filterable field")
        if isinstance(pred, Eq):
            return set(self.bitmaps[name].get(pred.value, ()))
        if isinstance(pred, OneOf):
            out: Set[int] = set()
            for v in pred.values:
                out |= self.bitmaps[name].get(v, set())
            return out
        if isinstance(pred, Range):
            values, ids = self._sorted_column(name)
            lo = 0 if pred.low is None else (
                bisect.bisect_left(values, pred.low) if pred.include_low
                else bisect.bisect_right(values, pred.low))
            hi = len(values) if pred.high is None else (
                bisect.bisect_right(values, pred.high) if pred.include_high
                else bisect.bisect_left(values, pred.high))
            return set(ids[lo:hi])
        raise TypeError(f"unsupported predicate {type(pred).__name__}")

    def matching_ids(self, where: Dict[str, Predicate]) -> Tuple[Set[int], int]:
        """Intersect predicates into an allowed-id set (the pushdown domain).

        Returns the raw set, not a `CandidateSet`: the planner wants exactly
        this and nothing else, and on a filter matching tens of thousands of
        documents, building a `{id: 1.0}` score map and converting it back to a
        set was one of the largest costs in the query path.
        """
        started = time.perf_counter()
        result: Optional[Set[int]] = None
        examined = 0
        # Cheapest (most selective) predicate first so the intersection shrinks fast.
        ordered = sorted(where.items(), key=lambda kv: self._selectivity(kv[0], kv[1]))
        for name, pred in ordered:
            matched = self._match(name, pred)
            examined += len(matched)
            result = matched if result is None else (result & matched)
            if not result:
                break
        if result is None:
            result = self.live()
        elif self.deleted:
            result = result - self.deleted
        self._last_latency_ms = (time.perf_counter() - started) * 1000.0
        return result, examined

    def filter(self, where: Dict[str, Predicate]) -> CandidateSet:
        ids, examined = self.matching_ids(where)
        return CandidateSet(scores=dict.fromkeys(ids, 1.0), source=self.name,
                            examined=examined)

    def _selectivity(self, name: str, pred: Predicate) -> float:
        """Fraction of the corpus a predicate is expected to keep (0..1)."""
        n = max(self.n_docs, 1)
        if isinstance(pred, Eq):
            return len(self.bitmaps[name].get(pred.value, ())) / n
        if isinstance(pred, OneOf):
            return sum(len(self.bitmaps[name].get(v, ())) for v in pred.values) / n
        return 0.5  # ponytail: ranges assumed mid-selective until measured

    # ---------------------------- interface -------------------------------- #
    def capabilities(self) -> Capabilities:
        return Capabilities(structured=True)

    def estimate(self, where: Dict[str, Predicate], budget: Budget) -> CostEstimate:
        n = max(self.n_docs, 1)
        selectivity = 1.0
        for name, pred in where.items():
            selectivity *= max(self._selectivity(name, pred), 1.0 / n)
        cardinality = max(int(n * selectivity), 0)
        latency_ms = self.base_ms + cardinality * self.sec_per_doc * 1000.0
        # Structured filtering is exact: it never loses a matching document.
        return CostEstimate(latency_ms=latency_ms, recall=1.0, cardinality=cardinality)

    def search(self, where: Dict[str, Predicate], budget: Budget) -> CandidateSet:
        cs = self.filter(where)
        if budget.domain is not None:
            cs.scores = {d: s for d, s in cs.scores.items() if d in budget.domain}
        return cs

    def calibrate(self, sample: List[Dict[str, Predicate]]) -> None:
        sample = [s for s in sample if s][:20]
        if not sample:
            return
        points = []
        for where in sample:
            best = float("inf")
            for _ in range(TIMING_REPEATS):
                started = time.perf_counter()
                matched = len(self.matching_ids(where)[0])
                best = min(best, time.perf_counter() - started)
            points.append((float(matched), best))
        base_s, slope_s = fit_linear(points)
        self.base_ms = base_s * 1000.0
        self.sec_per_doc = slope_s

    def statistics(self) -> Dict[str, Any]:
        return {
            "n_docs": self.n_docs,
            "fields": {f: len(self.bitmaps[f]) for f in self.fields},
            "sec_per_doc": self.sec_per_doc,
            "base_ms": self.base_ms,
        }
