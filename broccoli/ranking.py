"""Ranking and fusion (Information.md §7, SystemDesign.md §8).

RRF is the default because it is ~10 lines, needs no tuning, and is a
shockingly strong baseline. Weighted fusion is available when someone has
actually tuned weights on their own data.
"""

from __future__ import annotations

import heapq
from typing import Callable, Dict, List, Optional, Sequence

from .types import CandidateSet

RRF_K = 60  # the standard constant from the RRF paper


def normalize(scores: Dict[int, float]) -> Dict[int, float]:
    """Min-max to [0,1] so heterogeneous scores (BM25 vs cosine) are comparable."""
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi - lo < 1e-12:
        return {d: 1.0 for d in scores}
    return {d: (s - lo) / (hi - lo) for d, s in scores.items()}


def rrf(candidate_sets: Sequence[CandidateSet], k: int = RRF_K) -> Dict[int, float]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1/(k + rank(d))."""
    fused: Dict[int, float] = {}
    for cs in candidate_sets:
        ranked = sorted(cs.scores.items(), key=lambda kv: kv[1], reverse=True)
        for rank, (doc_id, _) in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


def weighted(candidate_sets: Sequence[CandidateSet],
             weights: Optional[Dict[str, float]] = None) -> Dict[int, float]:
    """Linear combination of min-max normalized scores, keyed by index name."""
    weights = weights or {}
    fused: Dict[int, float] = {}
    for cs in candidate_sets:
        w = weights.get(cs.source, 1.0)
        for doc_id, score in normalize(cs.scores).items():
            fused[doc_id] = fused.get(doc_id, 0.0) + w * score
    return fused


def fuse(candidate_sets: Sequence[CandidateSet], strategy: str = "rrf",
         weights: Optional[Dict[str, float]] = None) -> Dict[int, float]:
    sets = [cs for cs in candidate_sets if cs and len(cs)]
    if not sets:
        return {}
    if len(sets) == 1:
        return dict(sets[0].scores)
    if strategy == "weighted":
        return weighted(sets, weights)
    if strategy == "none":
        return dict(sets[0].scores)
    return rrf(sets)


def top_k(scores: Dict[int, float], k: int) -> List[tuple]:
    """Stable top-k: ties broken by doc id so results are deterministic.

    `nlargest` is O(n log k) against a full sort's O(n log n). That gap is the
    difference between ranking the answer and ranking the corpus — a filter-only
    query scores every surviving document, so a 100k-survivor filter was paying
    for a 100k-element sort to return 10 hits.
    """
    if k >= len(scores):
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return heapq.nlargest(k, scores.items(), key=lambda kv: (kv[1], -kv[0]))


def apply_recency(scores: Dict[int, float], timestamps: Dict[int, float],
                  now: float, half_life_s: float) -> Dict[int, float]:
    """Exponential recency decay — the temporal axis in its simplest useful form."""
    if half_life_s <= 0:
        return scores
    out = {}
    for doc_id, score in scores.items():
        ts = timestamps.get(doc_id)
        if ts is None:
            out[doc_id] = score
            continue
        age = max(now - ts, 0.0)
        out[doc_id] = score * (0.5 ** (age / half_life_s))
    return out


def calibrate(sizes: Sequence[int] = (256, 1024, 4096)) -> tuple:
    """Measure per-document fusion and top-k cost on THIS machine.

    Returns (fusion_ms_per_doc, rank_ms_per_doc). Both were hardcoded guesses
    before; on a candidate set of a few thousand docs they are a large enough
    share of query time to matter to plan choice.
    """
    import time

    fusion, rank = [], []
    for n in sizes:
        a = CandidateSet(source="a", scores={i: float(n - i) for i in range(n)})
        b = CandidateSet(source="b", scores={i: float(i) for i in range(n // 2, n)})
        started = time.perf_counter()
        fused = rrf([a, b])
        fusion.append(((time.perf_counter() - started) * 1000.0) / n)
        started = time.perf_counter()
        top_k(fused, 50)
        rank.append(((time.perf_counter() - started) * 1000.0) / n)
    fusion.sort()
    rank.sort()
    return fusion[len(fusion) // 2], rank[len(rank) // 2]


Reranker = Callable[[List[int]], Dict[int, float]]
"""Optional cross-encoder hook: takes doc ids, returns better scores.

Off by default — it buys precision and costs latency, so it is an explicit
plan choice the optimizer makes, never an implicit default.
"""
