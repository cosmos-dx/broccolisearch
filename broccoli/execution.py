"""Execution engine (SystemDesign.md §7).

Runs the plan the optimizer chose. Every operator honours its budget and emits
stage stats, which is what makes `explain` truthful and what feeds the
estimate-vs-actual loop.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from . import ranking
from .optimizer import QueryContext
from .query import Plan, StageStat
from .types import CandidateSet


class Executor:
    def __init__(self, lexical=None, vector=None, structured=None):
        self.lexical = lexical
        self.vector = vector
        self.structured = structured

    def run(self, plan: Plan, ctx: QueryContext,
            timestamps: Optional[Dict[int, float]] = None,
            reranker=None) -> Tuple[List[tuple], List[StageStat], float]:
        started = time.perf_counter()
        stages: List[StageStat] = []
        candidate_sets: List[CandidateSet] = []
        universe = ctx.n_docs

        for step in plan.steps:
            step_started = time.perf_counter()

            if step.op == "filter":
                # Already computed during featurization; charge its real cost
                # rather than paying for the same bitmap intersection twice.
                stages.append(StageStat("filter", universe, ctx.domain_size,
                                        ctx.filter_ms, ctx.domain_size))
                continue

            if step.op == "lexical":
                cs = self.lexical.search(ctx.terms, step.budget)
            elif step.op == "vector":
                cs = self.vector.search(ctx.query.semantic, step.budget)
            else:
                raise ValueError(f"unknown operator '{step.op}'")

            candidate_sets.append(cs)
            elapsed = (time.perf_counter() - step_started) * 1000.0
            n_in = ctx.domain_size if step.budget.domain is not None else universe
            stages.append(StageStat(step.op, n_in, len(cs), elapsed, cs.examined))

        # ---- fuse ----
        fuse_started = time.perf_counter()
        if candidate_sets:
            scores = ranking.fuse(candidate_sets, plan.fusion)
        else:
            scores = {d: 1.0 for d in (ctx.domain or set())}
        n_before = len(scores)

        # ---- temporal decay (optional) ----
        if ctx.query.decay and timestamps:
            from .query import parse_duration
            half_life = parse_duration(ctx.query.decay)
            scores = ranking.apply_recency(scores, timestamps, time.time(), half_life)

        # ---- optional rerank ----
        if reranker is not None and scores:
            head = [d for d, _ in ranking.top_k(scores, ctx.query.k * 2)]
            reranked = reranker(head)
            for doc_id, score in reranked.items():
                scores[doc_id] = score

        results = ranking.top_k(scores, ctx.query.k)
        stages.append(StageStat("rank", n_before, len(results),
                                (time.perf_counter() - fuse_started) * 1000.0, n_before))

        total_ms = (time.perf_counter() - started) * 1000.0 + ctx.filter_ms
        return results, stages, total_ms
