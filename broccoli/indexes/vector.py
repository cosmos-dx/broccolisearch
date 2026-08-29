"""Vector index: HNSW (rented from hnswlib) with an exact numpy path.

Two things here matter to the optimizer:

1. `ef` is the recall/latency dial, so `estimate()` returns a point on a
   MEASURED curve, not a guess (Information.md §4, SystemDesign.md §4.2).
2. When a pushed-down filter leaves few survivors, exact brute force over the
   survivors is both cheaper AND exact. Choosing between exact and approximate
   is a real cost-based decision, not a config flag.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Set

import numpy as np

from ..calibration import TIMING_REPEATS, fit_linear
from ..types import Budget, Capabilities, CandidateSet, CostEstimate
from . import BaseIndex

try:  # rented ANN engine; the index still works (exactly, slower) without it
    import hnswlib
    HAVE_HNSW = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_HNSW = False

_SPACE = {"cosine": "cosine", "l2": "l2", "ip": "ip"}

# Candidate count the latency curve is measured at. Costs for other candidate
# budgets are extrapolated from it via `sec_per_candidate`.
CALIB_K = 10


def _time_once(fn, *args) -> float:
    started = time.perf_counter()
    fn(*args)
    return time.perf_counter() - started


class VectorIndex(BaseIndex):
    name = "vector"

    def __init__(self, field: str, dim: int, metric: str = "cosine",
                 m: int = 16, ef_construction: int = 200,
                 deleted: Optional[Set[int]] = None):
        self.field = field
        self.dim = dim
        self.metric = metric
        self.m = m
        self.ef_construction = ef_construction
        self.deleted = deleted if deleted is not None else set()
        self._ids: List[int] = []
        self._rows: List[np.ndarray] = []
        self._matrix: Optional[np.ndarray] = None
        self._row_of: Dict[int, int] = {}
        self._row_lookup = np.empty(0, dtype=np.int64)
        self._ann = None
        self._dirty = True
        # Calibrated: ef -> {"latency_ms", "recall"}; and cost of an exact scan
        # as latency = base_ms + docs * sec_per_exact_doc.
        self.curve: Dict[int, Dict[str, float]] = {}
        self.sec_per_exact_doc = 6e-8
        self.base_ms = 0.01
        # Filtered ANN is far more expensive than plain ANN because hnswlib
        # calls a Python predicate for every node it visits, and the more
        # selective the filter the more nodes it rejects before finding k. The
        # penalty is measured, not guessed: penalty = a + b * (total / domain).
        self.filter_penalty_a = 1.0
        self.filter_penalty_b = 0.0
        # Cost of marshalling one hit into the scored dict both paths return.
        # Candidate budgets are ~5x k, so this is not a rounding error.
        self.sec_per_candidate = 2e-7

    # ------------------------------ build ---------------------------------- #
    def add(self, doc_id: int, vector: Sequence[float]) -> None:
        vec = np.asarray(vector, dtype=np.float32)
        if vec.shape != (self.dim,):
            raise ValueError(f"vector for doc {doc_id} has dim {vec.shape[0]}, "
                             f"expected {self.dim}")
        if doc_id in self._row_of:
            self._rows[self._row_of[doc_id]] = vec
        else:
            self._row_of[doc_id] = len(self._ids)
            self._ids.append(doc_id)
            self._rows.append(vec)
        self._dirty = True

    def remove(self, doc_id: int) -> None:
        # Tombstone only; physical removal happens on the next rebuild.
        self._dirty = True

    @property
    def n_docs(self) -> int:
        # Iterate the (normally tiny) tombstone set rather than materialising a
        # set of every indexed id: this is called from estimate() on the hot
        # planning path, where an O(corpus) allocation dominated everything else.
        if not self.deleted:
            return len(self._ids)
        return len(self._ids) - sum(1 for d in self.deleted if d in self._row_of)

    def _ensure_built(self) -> None:
        if not self._dirty:
            return
        if not self._ids:
            self._matrix = np.zeros((0, self.dim), dtype=np.float32)
            self._row_lookup = np.empty(0, dtype=np.int64)
            self._ann = None
            self._dirty = False
            return
        self._matrix = np.vstack(self._rows).astype(np.float32)
        # Dense doc_id -> row table (-1 = not in this index). Doc ids are dense
        # internal ints, so this is a plain gather instead of a per-document
        # dict lookup — `_rows_for` is the hottest thing in the query path.
        self._row_lookup = np.full(max(self._row_of) + 1, -1, dtype=np.int64)
        for doc_id, row in self._row_of.items():
            self._row_lookup[doc_id] = row
        if self.metric == "cosine":
            norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
            self._unit = self._matrix / np.maximum(norms, 1e-12)
        else:
            self._unit = self._matrix
        if HAVE_HNSW and len(self._ids) > 1:
            ann = hnswlib.Index(space=_SPACE[self.metric], dim=self.dim)
            ann.init_index(max_elements=len(self._ids),
                           ef_construction=self.ef_construction, M=self.m)
            ann.add_items(self._matrix, np.arange(len(self._ids)))
            self._ann = ann
        else:  # pragma: no cover - only when hnswlib is unavailable
            self._ann = None
        self._dirty = False

    # ---------------------------- scoring ---------------------------------- #
    def _similarity(self, query: np.ndarray, rows: np.ndarray) -> np.ndarray:
        if self.metric == "cosine":
            q = query / max(float(np.linalg.norm(query)), 1e-12)
            return rows @ q
        if self.metric == "ip":
            return rows @ query
        return -np.linalg.norm(rows - query, axis=1)  # l2: higher is better

    @staticmethod
    def _score_from_distance(metric: str, distance: float) -> float:
        if metric == "l2":
            return 1.0 / (1.0 + float(distance))
        return 1.0 - float(distance)

    def _marshal_ms(self, candidates: int) -> float:
        """Cost of turning raw hits into a scored CandidateSet.

        Both paths build a Python dict of `candidates` entries. Calibration
        measures at CALIB_K, so only the excess is charged here.
        """
        return max(candidates - CALIB_K, 0) * self.sec_per_candidate * 1000.0

    def _effective_ef(self, budget: Budget) -> int:
        """`_search_ann` widens ef to fit the candidate budget, so the estimate
        has to price the ef that will actually run, not the one requested."""
        return max(budget.ef, min(budget.candidates, max(self.n_docs, 1)))

    def _exact_latency_ms(self, domain_size: int, candidates: int) -> float:
        return (self.base_ms + domain_size * self.sec_per_exact_doc * 1000.0
                + self._marshal_ms(candidates))

    def _ann_latency_ms(self, budget: Budget, domain_size: int, total: int) -> float:
        latency = self._curve_at(self._effective_ef(budget))["latency_ms"]
        if domain_size < total:
            inverse_selectivity = total / max(domain_size, 1)
            penalty = self.filter_penalty_a + self.filter_penalty_b * inverse_selectivity
            latency *= max(penalty, 1.0)
            # A pushed-down domain is materialised as a row set before the walk.
            latency += domain_size * self.sec_per_exact_doc * 1000.0
        return latency + self._marshal_ms(budget.candidates)

    def _mode(self, budget: Budget) -> str:
        """Exact or approximate? Decided by COST, not by a magic threshold.

        An exhaustive scan over a small survivor set is both cheaper and exact,
        so when its estimated cost wins it strictly dominates the approximate
        path (recall 1.0 for less work). This is the same cost-based reasoning
        the query optimizer applies, just one level down.

        The optimizer's estimate and the executor's behaviour must agree, so
        both call this.
        """
        if not HAVE_HNSW or (self._ann is None and not self._dirty):
            return "exact"
        total = max(len(self._ids), 1)
        domain_size = len(budget.domain) if budget.domain is not None else total
        if domain_size <= max(budget.candidates, budget.k):
            return "exact"  # fewer survivors than we must return: scan them all
        if budget.domain is None:
            return "ann"
        exact = self._exact_latency_ms(domain_size, budget.candidates)
        ann = self._ann_latency_ms(budget, domain_size, total)
        return "exact" if exact <= ann else "ann"

    # ---------------------------- interface -------------------------------- #
    def capabilities(self) -> Capabilities:
        return Capabilities(vector=True)

    def estimate(self, vector: Sequence[float], budget: Budget) -> CostEstimate:
        self._ensure_built()
        total = max(self.n_docs, 1)
        domain_size = len(budget.domain) if budget.domain is not None else total
        if self._mode(budget) == "exact":
            # An exhaustive scan cannot miss a neighbour.
            return CostEstimate(
                latency_ms=self._exact_latency_ms(domain_size, budget.candidates),
                recall=1.0,
                cardinality=min(domain_size, budget.candidates))
        return CostEstimate(
            latency_ms=self._ann_latency_ms(budget, domain_size, total),
            recall=self._curve_at(self._effective_ef(budget))["recall"],
            cardinality=min(domain_size, budget.candidates))

    def _curve_at(self, ef: int) -> Dict[str, float]:
        """Nearest measured point on the recall/latency curve."""
        if not self.curve:
            # Un-calibrated fallback; deliberately pessimistic about recall.
            return {"latency_ms": self.base_ms
                    + max(self.n_docs, 1) * self.sec_per_exact_doc * 300.0,
                    "recall": min(0.99, 0.70 + 0.05 * float(np.log2(max(ef, 2))))}
        widest = max(self.curve)
        if ef > widest:
            # Beyond the calibrated ladder, HNSW cost grows ~linearly in ef.
            # Clamping to the widest measured point instead (the old behaviour)
            # made large candidate budgets look free.
            point = self.curve[widest]
            return {"latency_ms": point["latency_ms"] * (ef / widest),
                    "recall": min(1.0, point["recall"])}
        nearest = min(self.curve, key=lambda e: abs(e - ef))
        return self.curve[nearest]

    def search(self, vector: Sequence[float], budget: Budget) -> CandidateSet:
        started = time.perf_counter()
        self._ensure_built()
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (self.dim,):
            raise ValueError(f"query vector dim {query.shape[0]} != index dim {self.dim}")
        if not self._ids:
            return CandidateSet(scores={}, source=self.name, examined=0)

        k = max(1, min(budget.candidates, len(self._ids)))
        if self._mode(budget) == "exact":
            scores, examined = self._search_exact(query, budget, k)
        else:
            scores, examined = self._search_ann(query, budget, k)
        cs = CandidateSet(scores=scores, source=self.name, examined=examined)
        self._last_latency_ms = (time.perf_counter() - started) * 1000.0
        return cs

    def _rows_for(self, budget: Budget):
        """Restrict the scan to the pushed-down domain (minus deletions).

        Vectorised: the per-document dict lookups and membership tests this
        replaced were the single largest cost in the query path.
        """
        if budget.domain is None:
            if not self.deleted:
                return np.arange(len(self._ids), dtype=np.int64)
            live = [i for i, d in enumerate(self._ids) if d not in self.deleted]
            return np.asarray(live, dtype=np.int64)

        domain = budget.domain
        if self.deleted:
            domain = domain - self.deleted
        ids = np.fromiter(domain, dtype=np.int64, count=len(domain))
        if ids.size:
            ids = ids[ids < self._row_lookup.size]
        if not ids.size:
            return np.empty(0, dtype=np.int64)
        rows = self._row_lookup[ids]
        return rows[rows >= 0]

    def _search_exact(self, query: np.ndarray, budget: Budget, k: int):
        rows = self._rows_for(budget)
        if rows.size == 0:
            return {}, 0
        sims = self._similarity(query, self._unit[rows])
        k = min(k, rows.size)
        top = np.argpartition(-sims, k - 1)[:k] if k < rows.size else np.arange(rows.size)
        top = top[np.argsort(-sims[top])]
        # Convert both sides to Python lists in one C call each: indexing numpy
        # scalar-by-scalar inside a dict comprehension costs more than the scan.
        ids = self._ids
        scores = dict(zip([ids[r] for r in rows[top].tolist()],
                          sims[top].tolist()))
        return scores, int(rows.size)

    def _search_ann(self, query: np.ndarray, budget: Budget, k: int):
        self._ann.set_ef(max(budget.ef, k))
        allowed_rows = None
        if budget.domain is not None or self.deleted:
            # .tolist() converts in C and yields real Python ints, which is
            # what the predicate compares against; a genexp with int() per row
            # does the same work one interpreter step at a time.
            allowed_rows = set(self._rows_for(budget).tolist())
            if not allowed_rows:
                return {}, 0
            predicate = lambda row: row in allowed_rows  # noqa: E731
        else:
            predicate = None
        try:
            labels, distances = self._ann.knn_query(
                query.reshape(1, -1), k=min(k, len(self._ids)), filter=predicate)
        except RuntimeError:
            # hnswlib raises when it cannot find k elements under the filter.
            return self._search_exact(query, budget, k)
        ids = self._ids
        scores = {ids[row]: self._score_from_distance(self.metric, dist)
                  for row, dist in zip(labels[0].tolist(), distances[0].tolist())}
        # Work units must be COMPARABLE to the exact path (which reports one
        # unit per distance computation), or ANN looks artificially free. An
        # HNSW search keeps ~ef candidates and computes distances to each one's
        # neighbours, so visited-node count is roughly ef * M.
        return scores, int(max(budget.ef, k) * self.m)

    # --------------------------- calibration ------------------------------- #
    def calibrate(self, efs: Sequence[int] = (16, 32, 64, 128, 256),
                  n_samples: int = 20, k: int = CALIB_K) -> None:
        """Measure the real recall/latency curve on THIS corpus and machine.

        Without this the optimizer is confidently wrong: ANN recall is entirely
        dataset-dependent (Approach.md §3 — calibration is not optional).
        """
        self._ensure_built()
        n = len(self._ids)
        if n < 2:
            return
        rng = np.random.default_rng(0)
        sample_rows = rng.choice(n, size=min(n_samples, n), replace=False)
        queries = self._matrix[sample_rows]

        # Ground truth = exhaustive scan. Time the REAL exact path over several
        # domain sizes so the fit separates fixed cost from per-document cost.
        truth = []
        points = []
        # Shuffle: a real filter yields ids scattered across the matrix, so a
        # contiguous prefix would time a cheaper memory gather than execution
        # ever performs. Calibrate on the access pattern you actually run.
        all_ids = list(self._row_of)
        rng.shuffle(all_ids)
        for size in (max(n // 16, 1), max(n // 8, 1), max(n // 4, 1),
                     max(n // 2, 1), n):
            # Build the domain outside the timer: constructing it is the
            # structured index's cost, not the vector index's.
            domain = set(all_ids[:size])
            budget = Budget(candidates=k, ef=k, domain=domain)
            for q in queries[:6]:
                best = min(_time_once(self._search_exact, q, budget, k)
                           for _ in range(TIMING_REPEATS))
                points.append((float(size), best))
        base_s, slope_s = fit_linear(points)
        self.base_ms = base_s * 1000.0
        self.sec_per_exact_doc = slope_s

        for q in queries:
            sims = self._similarity(q, self._unit)
            kk = min(k, n)
            top = np.argpartition(-sims, kk - 1)[:kk]
            truth.append(set(int(t) for t in top))

        if self._ann is None:  # pragma: no cover - hnswlib missing
            return
        self.curve = {}
        for ef in efs:
            self._ann.set_ef(max(ef, k))
            # Time ONE query at a time: execution never batches, and batching
            # here would amortize away per-call overhead the optimizer must see.
            latencies = []
            hits = 0
            budget = Budget(candidates=k, ef=ef)
            for q, t in zip(queries, truth):
                latencies.append(min(_time_once(self._search_ann, q, budget, k)
                                     for _ in range(TIMING_REPEATS)))
                labels, _ = self._ann.knn_query(q.reshape(1, -1), k=min(k, n))
                hits += len(set(int(x) for x in labels[0]) & t)
            self.curve[int(ef)] = {
                "latency_ms": (sum(latencies) / len(latencies)) * 1000.0,
                "recall": hits / max(len(queries) * min(k, n), 1),
            }

        self._calibrate_marshalling(queries, n)
        self._calibrate_filter_penalty(queries, k, n, all_ids)

    def _calibrate_marshalling(self, queries, n: int) -> None:
        """Measure per-candidate cost by varying k with the domain held fixed.

        Everything else in the model is a function of domain size and ef, so
        without this term a 250-candidate request is priced like a 10-candidate
        one — and the candidate budget is normally 5x k.
        """
        budget = Budget(candidates=n, ef=n, k=n)
        points = []
        for cand in (CALIB_K, 100, 400, 1000):
            if cand > n:
                continue
            for q in queries[:4]:
                best = min(_time_once(self._search_exact, q, budget, cand)
                           for _ in range(TIMING_REPEATS))
                points.append((float(cand), best))
        if len(points) >= 2:
            _, slope_s = fit_linear(points)
            self.sec_per_candidate = max(slope_s, 0.0)

    def _calibrate_filter_penalty(self, queries, k: int, n: int, all_ids) -> None:
        """Measure how much a pushed-down filter slows the ANN walk.

        The old model guessed `sqrt(total/domain)` capped at 4x. Measurement
        showed the real penalty is far larger, which made the optimizer choose
        filtered ANN when an exact scan would have been several times faster.
        """
        ref_ef = 64 if 64 in self.curve else sorted(self.curve)[0]
        baseline = max(self.curve[ref_ef]["latency_ms"], 1e-9)
        points = []
        for fraction in (0.5, 0.25, 0.1, 0.05):
            size = max(int(n * fraction), k + 1)
            if size >= n:
                continue
            domain = set(all_ids[:size])
            budget = Budget(candidates=k, ef=ref_ef, domain=domain, k=k)
            for q in queries[:4]:
                elapsed = min(_time_once(self._search_ann, q, budget, k)
                              for _ in range(TIMING_REPEATS))
                points.append((n / size, (elapsed * 1000.0) / baseline))
        if len(points) >= 2:
            a, b = fit_linear(points)
            self.filter_penalty_a = max(a, 1.0)
            self.filter_penalty_b = max(b, 0.0)

    def statistics(self) -> Dict[str, Any]:
        return {"n_docs": self.n_docs, "dim": self.dim, "metric": self.metric,
                "curve": self.curve, "sec_per_exact_doc": self.sec_per_exact_doc,
                "base_ms": self.base_ms, "ann": bool(self._ann),
                "filter_penalty": [self.filter_penalty_a, self.filter_penalty_b]}
