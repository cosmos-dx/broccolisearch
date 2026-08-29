"""The cost-based query optimizer (SystemDesign.md §6) — the point of the project.

    featurize -> enumerate plans -> estimate each -> Policy.choose -> Plan

The two ideas that make this different from a rule-based search planner:

1. Cost is a (latency, recall) PAIR. Approximate operators expose a curve, so
   the planner rides it: pick the SMALLEST `ef` that still meets the recall
   target instead of a hardcoded one.
2. Filter push-down is driven by measured cardinality. A selective filter
   shrinks the domain, which can flip the vector index from approximate to
   exact — cheaper AND better. That decision falls straight out of the stats.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from .indexes import LexicalIndex, StructuredIndex, VectorIndex
from .query import Plan, PlanEstimate, PlanStep, Query
from .stats import StatisticsStore
from .types import Budget

EF_LADDER = (16, 32, 64, 128, 256)
OVERFETCH = 5          # candidate-stage overfetch multiple of k
MAX_CANDIDATES = 2000
FUSION_MS_PER_DOC = 1.2e-4
RANK_MS_PER_DOC = 4e-5


@dataclass
class QueryContext:
    """Everything the planner learned about this query, reused by the executor
    so no work is done twice."""

    query: Query
    terms: List[str] = field(default_factory=list)
    domain: Optional[Set[int]] = None      # pushed-down filter result
    domain_size: int = 0
    n_docs: int = 0
    filter_ms: float = 0.0
    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def selectivity(self) -> float:
        return self.domain_size / max(self.n_docs, 1)


# ------------------------------- policies ---------------------------------- #


class Policy(ABC):
    """Chooses among enumerated plans. Swapping this for a learned policy is
    the entire 'adaptive' upgrade path — nothing above it changes."""

    name = "policy"

    @abstractmethod
    def choose(self, candidates: Sequence[Plan], ctx: QueryContext) -> Plan:
        ...

    def observe(self, plan: Plan, ctx: QueryContext,
                actual_latency_ms: float, n_results: int) -> None:
        """Feedback hook. The rule-based policy ignores it; a learned one won't."""


class RuleBasedPolicy(Policy):
    """Minimum estimated latency subject to meeting the recall target.

    ponytail: hand-tuned rules, no learning. Ceiling: thresholds generalize
    imperfectly across workloads. Upgrade path: LearnedPolicy trained on the
    query history that `StatisticsStore` is already collecting.
    """

    name = "rule_based"

    def choose(self, candidates: Sequence[Plan], ctx: QueryContext) -> Plan:
        if not candidates:
            raise ValueError("no executable plan for this query")
        target = ctx.query.recall_target
        meeting = [p for p in candidates if p.estimate.recall >= target]
        if meeting:
            return min(meeting, key=lambda p: p.estimate.latency_ms)
        # Nothing reaches the target: best-effort = highest recall, ties by speed.
        return max(candidates, key=lambda p: (p.estimate.recall,
                                              -p.estimate.latency_ms))


class LearnedPolicy(Policy):
    """Ranks plans by MEASURED relevance coverage instead of operator fidelity.

    `RuleBasedPolicy` ranks by `estimate.recall`, which each index computes
    about *itself*: "did I return the true nearest neighbours / all the
    postings". An exact vector scan reports 1.0 there, so no plan can outrank
    it — even on corpora where fusing with BM25 demonstrably retrieves more
    relevant documents (measured on BEIR; see README and Research.md §7.3).

    Fidelity is not relevance, and crucially no index can report relevance
    about itself: fusion's gain comes from two indexes DISAGREEING about which
    documents matter, which neither can observe alone. So this policy stops
    deriving it and measures it — `fit` runs each plan shape over judged
    queries and records the relevance recall@k it actually achieved, bucketed
    by a query feature so the answer can differ per query rather than
    collapsing to "always fuse".

    ponytail: a bucketed mean over one feature is the smallest model that can
    represent "fusion helps here, not there". Ceiling: it conditions on the
    rarest query term alone and needs judgments to train. Upgrade path: a tree
    over the whole `QueryContext.features` dict, trained on click feedback
    arriving through `observe` so no judgments are required.
    """

    name = "learned"

    DF_EDGES = (1, 8, 64)

    def __init__(self, fallback: Optional[Policy] = None, min_samples: int = 5,
                 tolerance: float = 0.01):
        # bucket -> plan name -> mean quality achieved on the training queries
        self.coverage: Dict[str, Dict[str, float]] = {}
        self.support: Dict[str, int] = {}      # bucket -> training queries
        self.fallback = fallback or RuleBasedPolicy()
        self.min_samples = min_samples
        # How much quality a plan may give up to be preferred for being cheaper.
        # Without this the policy just maximises quality and always fuses, which
        # is a fixed strategy wearing a learned hat.
        self.tolerance = tolerance

    @classmethod
    def bucket(cls, features: Dict[str, Any]) -> str:
        """Log-bucketed document frequency of the rarest query term.

        The cheapest available proxy for "can the lexical index answer this on
        its own": a term in a handful of documents is a precise selector, one
        in half the corpus is not, and a term the corpus has never seen (df 0)
        means lexical contributes nothing and only the vector side can help.
        """
        if not features.get("has_text"):
            return "no_text"
        df = features.get("min_df") or 0
        for edge in cls.DF_EDGES:
            if df < edge:
                return f"min_df<{edge}"
        return f"min_df>={cls.DF_EDGES[-1]}"

    def fit(self, index, judgments, k: int = 10,
            strategies: Sequence[str] = ("lexical", "vector", "hybrid_rrf"),
            metric: Optional[Any] = None) -> "LearnedPolicy":
        """Measure each plan shape's achieved quality on judged queries.

        Defaults to nDCG@k, not recall@k, because they disagree and nDCG is
        what gets reported: on BEIR SciFact the vector plan has clearly worse
        recall than fusion yet nearly the same nDCG, because it ranks the hits
        it does find better. Training on recall therefore buys fusion's extra
        hits at 5x the work for no measurable gain in the metric anyone reads.

        Train on a DISJOINT set from whatever you evaluate on — a bucketed mean
        will happily memorise the queries it was fitted to.
        """
        from .eval import ndcg_at_k         # local: eval imports nothing from here
        score = metric or (lambda ids, j: ndcg_at_k(ids, j.relevant, k))

        totals: Dict[str, Dict[str, List[float]]] = {}
        # Probe queries are calibration traffic, not user traffic; letting them
        # into the history would bias every statistic read off it later.
        history = list(index.stats.history)
        try:
            for judgment in judgments:
                relevant = judgment.relevant_set
                if not relevant:
                    continue
                for strategy in strategies:
                    kwargs = dict(judgment.query)
                    kwargs["k"] = k
                    kwargs["pin"] = strategy
                    try:
                        results = index.search(**kwargs)
                    except ValueError:
                        continue          # strategy inapplicable to this query
                    # Read features off the history entry the search just wrote,
                    # so training buckets come from the same featurize() the
                    # optimizer will call at query time.
                    bucket = self.bucket(index.stats.history[-1].features)
                    slot = totals.setdefault(bucket, {}).setdefault(strategy, [])
                    slot.append(score(results.ids, judgment))
        finally:
            index.stats.history = history

        self.coverage = {
            bucket: {name: sum(scores) / len(scores)
                     for name, scores in plans.items()
                     if len(scores) >= self.min_samples}
            for bucket, plans in totals.items()
        }
        self.coverage = {b: p for b, p in self.coverage.items() if p}
        self.support = {b: max((len(s) for s in plans.values()), default=0)
                        for b, plans in totals.items()}
        return self

    def choose(self, candidates: Sequence[Plan], ctx: QueryContext) -> Plan:
        if not candidates:
            raise ValueError("no executable plan for this query")
        learned = self.coverage.get(self.bucket(ctx.features), {})
        known = [p for p in candidates if p.name in learned]
        if not known:
            # Unseen bucket or untrained policy: the rules are a worse objective,
            # not a broken one, and guessing from no evidence would be worse still.
            return self.fallback.choose(candidates, ctx)

        # Cheapest plan that is not measurably worse than the best one. Picking
        # the argmax instead would always fuse: fusion nearly always wins by a
        # hair, and paying 5x the work for a hair is the failure this policy is
        # supposed to fix, not a different flavour of it.
        best = max(learned[p.name] for p in known)
        good_enough = [p for p in known
                       if learned[p.name] >= best - self.tolerance]
        return min(good_enough, key=lambda p: p.estimate.latency_ms)

    def describe(self) -> str:
        if not self.coverage:
            return "learned policy: untrained"
        lines = ["learned policy: measured quality by query bucket"]
        for bucket in sorted(self.coverage):
            plans = self.coverage[bucket]
            best = max(plans, key=plans.get)
            body = "  ".join(f"{n}={v:.3f}" for n, v in sorted(plans.items()))
            lines.append(f"  {bucket:<14} n={self.support.get(bucket, 0):<4} "
                         f"{body}   -> prefers {best}")
        return "\n".join(lines)


# ------------------------------- optimizer --------------------------------- #


class Optimizer:
    def __init__(self, lexical: Optional[LexicalIndex] = None,
                 vector: Optional[VectorIndex] = None,
                 structured: Optional[StructuredIndex] = None,
                 stats: Optional[StatisticsStore] = None,
                 policy: Optional[Policy] = None):
        self.lexical = lexical
        self.vector = vector
        self.structured = structured
        self.stats = stats or StatisticsStore()
        self.policy = policy or RuleBasedPolicy()
        # Per-document fusion/ranking costs. Overwritten by measurement during
        # Index.calibrate(); these defaults only apply to an uncalibrated index.
        self.fusion_ms_per_doc = FUSION_MS_PER_DOC
        self.rank_ms_per_doc = RANK_MS_PER_DOC
        self.pipeline_ms = 0.0

    # ----------------------------- featurize ------------------------------- #
    def featurize(self, query: Query, n_docs: int) -> QueryContext:
        ctx = QueryContext(query=query, n_docs=n_docs)

        if query.has_text and self.lexical is not None:
            ctx.terms = self.lexical.analyze_query(query.text)

        # Resolve the filter NOW: bitmap intersection is cheap and yields EXACT
        # selectivity, which is the single most valuable input to the cost model.
        if query.has_filter and self.structured is not None:
            started = time.perf_counter()
            ctx.domain, _ = self.structured.matching_ids(query.where)
            ctx.filter_ms = (time.perf_counter() - started) * 1000.0
            ctx.domain_size = len(ctx.domain)
        elif self.structured is not None and self.structured.deleted:
            ctx.domain = self.structured.live()
            ctx.domain_size = len(ctx.domain)
        else:
            ctx.domain_size = n_docs

        dfs = [self.lexical.df(t) for t in ctx.terms] if self.lexical else []
        ctx.features = {
            "has_text": query.has_text,
            "has_semantic": query.has_semantic,
            "has_filter": query.has_filter,
            "n_terms": len(ctx.terms),
            "min_df": min(dfs) if dfs else 0,
            "max_df": max(dfs) if dfs else 0,
            "selectivity": round(ctx.selectivity, 4),
            "k": query.k,
            "recall_target": query.recall_target,
        }
        return ctx

    # ----------------------------- enumerate ------------------------------- #
    def _candidate_budget(self, k: int) -> int:
        return int(max(k, min(k * OVERFETCH, MAX_CANDIDATES)))

    def _vector_budget(self, ctx: QueryContext, n_candidates: int) -> Budget:
        """Ride the recall/latency curve: smallest ef that meets the target."""
        domain = ctx.domain
        target = ctx.query.recall_target
        k = ctx.query.k
        best = Budget(candidates=n_candidates, ef=EF_LADDER[-1], domain=domain, k=k)
        for ef in EF_LADDER:
            budget = Budget(candidates=n_candidates, ef=ef, domain=domain, k=k)
            est = self.vector.estimate(ctx.query.semantic, budget)
            if est.recall >= target:
                return budget
        return best

    def enumerate_plans(self, ctx: QueryContext) -> List[Plan]:
        q = ctx.query
        n_cand = self._candidate_budget(q.k)
        domain = ctx.domain
        plans: List[Plan] = []

        can_lex = self.lexical is not None and bool(ctx.terms)
        can_vec = self.vector is not None and q.has_semantic
        has_filter = q.has_filter and self.structured is not None

        filter_step = (PlanStep("filter", Budget(candidates=n_cand, k=q.k),
                                {"kept": ctx.domain_size})
                       if has_filter else None)

        def build(name: str, retrieval: List[PlanStep], fusion: str,
                  ranker: str) -> Plan:
            steps = ([filter_step] if filter_step else []) + retrieval
            return Plan(name=name, steps=steps, fusion=fusion, ranker=ranker)

        lex_budget = Budget(candidates=n_cand, domain=domain, k=q.k)

        # Walk the ef ladder once per query, not once per candidate plan.
        vec_budget = self._vector_budget(ctx, n_cand) if can_vec else None

        if can_lex:
            plans.append(build("lexical", [PlanStep("lexical", lex_budget)],
                               "none", "bm25"))
        if can_vec:
            plans.append(build("vector", [PlanStep("vector", vec_budget)],
                               "none", "vector"))
        if can_lex and can_vec:
            plans.append(build("hybrid_rrf",
                               [PlanStep("lexical", lex_budget),
                                PlanStep("vector", vec_budget)],
                               "rrf", "fusion"))
        if not plans and has_filter:
            # Filter-only query: the structured index IS the answer.
            plans.append(build("filter_only", [], "none", "none"))

        for plan in plans:
            plan.estimate = self.estimate_plan(plan, ctx)
        return plans

    # ------------------------------ cost model ----------------------------- #
    def estimate_plan(self, plan: Plan, ctx: QueryContext) -> PlanEstimate:
        # Constant across candidate plans, so it never changes which plan wins;
        # it makes the absolute number honest for `latency_budget_ms`.
        latency = self.pipeline_ms
        recalls: List[float] = []
        retrieved = 0
        n_retrieval_steps = 0

        for step in plan.steps:
            if step.op == "filter":
                # The filter already RAN during featurization, so its cost and
                # cardinality are observed facts, not predictions. Estimating
                # them again would import avoidable error (and the independence
                # assumption behind multiplied selectivities) for no benefit.
                latency += ctx.filter_ms
            elif step.op in ("lexical", "vector"):
                est = (self.lexical.estimate(ctx.terms, step.budget)
                       if step.op == "lexical"
                       else self.vector.estimate(ctx.query.semantic, step.budget))
                latency += est.latency_ms
                recalls.append(est.recall)
                # Retrieval stages EMIT candidates; a filter does not. Folding
                # the filter's survivor count in here charged every filtered
                # plan for ranking the whole domain instead of the handful of
                # candidates retrieved from it — which penalised exactly the
                # push-down the optimizer is supposed to favour.
                retrieved += est.cardinality
                n_retrieval_steps += 1

        # No retrieval stage means the filter IS the answer, and the executor
        # ranks every survivor.
        n_fuse = retrieved if n_retrieval_steps else ctx.domain_size
        if plan.fusion != "none":
            latency += n_fuse * self.fusion_ms_per_doc
        latency += n_fuse * self.rank_ms_per_doc

        if not recalls:
            recall = 1.0 if plan.name == "filter_only" else 0.0
        elif len(recalls) == 1:
            recall = recalls[0]
        else:
            # ponytail: independence assumption for union recall. Ceiling: two
            # indexes that fail on the SAME documents make this optimistic.
            # Upgrade path: learn the joint term from query history.
            missed = 1.0
            for r in recalls:
                missed *= (1.0 - r)
            recall = min(0.99, 1.0 - missed)

        return PlanEstimate(latency_ms=latency, recall=recall)

    # -------------------------------- plan --------------------------------- #
    def plan(self, query: Query, n_docs: int):
        """Returns (chosen_plan, context, considered_plan_names)."""
        ctx = self.featurize(query, n_docs)
        candidates = self.enumerate_plans(ctx)
        if not candidates:
            raise ValueError(
                "query has no usable intent: provide text=, semantic=, or where=")

        if query.pin:
            pinned = [p for p in candidates if p.name == query.pin]
            if not pinned:
                raise ValueError(
                    f"pinned plan '{query.pin}' unavailable; "
                    f"options: {[p.name for p in candidates]}")
            chosen = pinned[0]
        else:
            chosen = self.policy.choose(candidates, ctx)

        considered = [f"{p.name}(~{p.estimate.latency_ms:.2f}ms,"
                      f"r~{p.estimate.recall:.2f})" for p in candidates]
        return chosen, ctx, considered
