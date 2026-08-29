#!/usr/bin/env python3
"""
BroccoliSearch — minimal thesis prototype.

Proves (or falsifies) the core bet from Research.md §8 / SHAPE.md:

    A per-query, cost-based optimizer that ROUTES across lexical / vector /
    filter-then-vector strategies beats any single FIXED strategy on
    latency-at-fixed-recall over a mixed workload.

This is deliberately NOT the real engine. Per Approach.md's reuse ladder we do
NOT rebuild Tantivy/HNSW/roaring to prove a thesis. This is a self-contained
simulation (stdlib only, deterministic) whose only job is to show whether the
routing idea holds. If it holds, we scale it into the Rust engine
(Architecture.md). If it doesn't, we learned that in minutes.

Run:   python3 experiments/thesis_prototype.py
The script ends in an assert-based self-check (Approach.md §3): running it IS
the check.

ponytail: cost is modeled as "work units" (posting-list length scanned for
lexical, #distance comparisons for vector). Ceiling: it's a simulation, not
wall-clock on real ANN. Upgrade path: replace strategy bodies with the real
broccoli-index engines and re-run the same harness (broccoli-eval).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# ----------------------------- config -------------------------------------- #

SEED = 0
N_DOCS = 2000
N_CONCEPTS = 40          # ~50 docs/concept
N_CATEGORIES = 5
DIM = 16
NOISE = 0.15            # embedding noise vs. concept centroid
CANONICAL_PROB = 0.7   # P(a concept doc contains the concept's canonical token)
N_SYNONYMS = 5
QUERIES_PER_TYPE = 60
K = 50                 # retrieve top-K
RECALL_TARGET = 0.80   # the fixed recall we compare latency at
MIN_DF = 10            # router: lexical only if term is common enough to have recall
MAX_DF = 500           # router: ...but specific enough to be precise

# --------------------------- data model ------------------------------------ #


@dataclass
class Doc:
    id: int
    concept: int
    category: int
    vec: list[float]
    tokens: set  # set of token ids


@dataclass
class Query:
    kind: str                 # 'keyword' | 'semantic' | 'filtered'  (for reporting only)
    text_token: object        # a token id the caller typed
    semantic_vec: list[float]
    filter_cat: int | None    # structured filter
    ground_truth: set = field(default_factory=set)


# --------------------------- corpus build ---------------------------------- #


def gauss_vec(center: list[float], sigma: float, rnd: random.Random) -> list[float]:
    return [c + rnd.gauss(0.0, sigma) for c in center]


def build_corpus(rnd: random.Random):
    # well-separated random centroids (near-orthogonal in 16-d)
    centroids = [[rnd.gauss(0, 1) for _ in range(DIM)] for _ in range(N_CONCEPTS)]
    canonical = [("canon", c) for c in range(N_CONCEPTS)]
    synonyms = [[("syn", c, j) for j in range(N_SYNONYMS)] for c in range(N_CONCEPTS)]

    docs: list[Doc] = []
    for i in range(N_DOCS):
        c = rnd.randrange(N_CONCEPTS)
        cat = rnd.randrange(N_CATEGORIES)
        vec = gauss_vec(centroids[c], NOISE, rnd)
        toks: set = set()
        if rnd.random() < CANONICAL_PROB:
            toks.add(canonical[c])
        else:
            toks.add(rnd.choice(synonyms[c]))
        # a couple of random filler tokens (noise vocabulary)
        for _ in range(3):
            toks.add(("filler", rnd.randrange(200)))
        docs.append(Doc(i, c, cat, vec, toks))

    return docs, centroids, canonical, synonyms


def build_indexes(docs: list[Doc]):
    postings: dict = {}                       # token -> set(doc_id)   (inverted index)
    cat_bitmap: dict = {k: set() for k in range(N_CATEGORIES)}  # structured "bitmap"
    for d in docs:
        for t in d.tokens:
            postings.setdefault(t, set()).add(d.id)
        cat_bitmap[d.category].add(d.id)
    return postings, cat_bitmap


# --------------------------- query build ----------------------------------- #


def make_queries(docs, centroids, canonical, synonyms, rnd):
    by_concept: dict = {}
    for d in docs:
        by_concept.setdefault(d.concept, []).append(d)

    queries: list[Query] = []

    def concept_with_docs():
        while True:
            c = rnd.randrange(N_CONCEPTS)
            if by_concept.get(c):
                return c

    # keyword: caller typed the canonical term; wants docs that literally have it
    for _ in range(QUERIES_PER_TYPE):
        c = concept_with_docs()
        gt = {d.id for d in by_concept[c] if canonical[c] in d.tokens}
        if not gt:
            continue
        q = Query("keyword", canonical[c], gauss_vec(centroids[c], NOISE, rnd), None, gt)
        queries.append(q)

    # semantic: caller's word is a rare synonym; true intent is the whole concept
    for _ in range(QUERIES_PER_TYPE):
        c = concept_with_docs()
        gt = {d.id for d in by_concept[c]}
        q = Query("semantic", rnd.choice(synonyms[c]),
                  gauss_vec(centroids[c], NOISE, rnd), None, gt)
        queries.append(q)

    # filtered: semantic intent constrained to a category
    for _ in range(QUERIES_PER_TYPE):
        c = concept_with_docs()
        cat = rnd.randrange(N_CATEGORIES)
        gt = {d.id for d in by_concept[c] if d.category == cat}
        if not gt:
            continue
        q = Query("filtered", rnd.choice(synonyms[c]),
                  gauss_vec(centroids[c], NOISE, rnd), cat, gt)
        queries.append(q)

    return queries


# --------------------------- strategies ------------------------------------ #
# Each returns (retrieved_ids, cost_work_units).


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-12)


def strat_lexical(q: Query, docs, postings, cat_bitmap):
    hits = set(postings.get(q.text_token, ()))          # scan posting list
    cost = len(postings.get(q.text_token, ()))
    if q.filter_cat is not None:
        hits &= cat_bitmap[q.filter_cat]
        cost += len(cat_bitmap[q.filter_cat])
    retrieved = list(hits)[:K]
    return retrieved, max(cost, 1)


def _vector_over(domain_ids, q: Query, docs):
    scored = sorted(domain_ids, key=lambda i: cosine(q.semantic_vec, docs[i].vec),
                    reverse=True)
    return scored[:K], max(len(domain_ids), 1)          # cost = #distance comparisons


def strat_vector(q: Query, docs, postings, cat_bitmap):
    return _vector_over(range(len(docs)), q, docs)


def strat_filter_then_vector(q: Query, docs, postings, cat_bitmap):
    if q.filter_cat is not None:
        domain = cat_bitmap[q.filter_cat]               # push filter down first
    else:
        domain = range(len(docs))                       # no filter → full scan
    return _vector_over(domain, q, docs)


# --------------------------- the optimizer --------------------------------- #


def optimizer_route(q: Query, postings, cat_bitmap):
    """Cost-based router. Uses ONLY observable features (filter presence +
    selectivity, and the query term's document frequency). It never inspects
    ground truth. This tiny rule set IS the thesis in miniature."""
    df = len(postings.get(q.text_token, ()))

    # 1. Selective structured filter? Push it down, then vector over survivors.
    if q.filter_cat is not None:
        return "filter_then_vector"
    # 2. Term specific enough to be precise but common enough to have recall?
    if MIN_DF <= df <= MAX_DF:
        return "lexical"
    # 3. Otherwise it's a semantic intent → vector.
    return "vector"


STRATS = {
    "lexical": strat_lexical,
    "vector": strat_vector,
    "filter_then_vector": strat_filter_then_vector,
}


def strat_adaptive(q, docs, postings, cat_bitmap):
    choice = optimizer_route(q, postings, cat_bitmap)
    return STRATS[choice](q, docs, postings, cat_bitmap)


# --------------------------- evaluation ------------------------------------ #


def recall(retrieved, ground_truth) -> float:
    if not ground_truth:
        return 1.0
    return len(set(retrieved) & ground_truth) / len(ground_truth)


def evaluate(name, fn, queries, docs, postings, cat_bitmap):
    recalls, costs = [], []
    for q in queries:
        ret, cost = fn(q, docs, postings, cat_bitmap)
        recalls.append(recall(ret, q.ground_truth))
        costs.append(cost)
    return {
        "name": name,
        "recall": sum(recalls) / len(recalls),
        "cost": sum(costs) / len(costs),
    }


def main():
    rnd = random.Random(SEED)
    docs, centroids, canonical, synonyms = build_corpus(rnd)
    postings, cat_bitmap = build_indexes(docs)
    queries = make_queries(docs, centroids, canonical, synonyms, rnd)

    contenders = {
        "lexical_only": strat_lexical,
        "vector_only": strat_vector,
        "filter_then_vector_only": strat_filter_then_vector,
        "ADAPTIVE (optimizer)": strat_adaptive,
    }
    results = [evaluate(n, f, queries, docs, postings, cat_bitmap)
               for n, f in contenders.items()]

    # ------- report -------
    print(f"\nBroccoliSearch thesis prototype  |  {len(queries)} queries, "
          f"{N_DOCS} docs, recall target = {RECALL_TARGET:.2f}\n")
    print(f"{'strategy':<26}{'mean recall':>13}{'mean cost':>12}"
          f"{'meets target?':>16}")
    print("-" * 67)
    for r in results:
        meets = "yes" if r["recall"] >= RECALL_TARGET else "NO"
        print(f"{r['name']:<26}{r['recall']:>13.3f}{r['cost']:>12.1f}{meets:>16}")

    adaptive = next(r for r in results if r["name"].startswith("ADAPTIVE"))
    fixed = [r for r in results if not r["name"].startswith("ADAPTIVE")]

    # cheapest fixed strategy that ALSO meets the recall target
    meeting = [r for r in fixed if r["recall"] >= RECALL_TARGET]
    best_fixed = min(meeting, key=lambda r: r["cost"]) if meeting else None
    print("-" * 67)
    if best_fixed:
        speedup = best_fixed["cost"] / adaptive["cost"]
        print(f"\nAdaptive cost {adaptive['cost']:.1f} vs best fixed "
              f"({best_fixed['name']}) {best_fixed['cost']:.1f}  "
              f"→ {speedup:.2f}x cheaper at equal recall.\n")

    # ------- self-check (Approach.md §3): the thesis must hold -------
    # 1. The optimizer must itself meet the recall target.
    assert adaptive["recall"] >= RECALL_TARGET, \
        f"adaptive recall {adaptive['recall']:.3f} < target {RECALL_TARGET}"
    # 2. No fixed strategy may dominate the optimizer: there must be no single
    #    fixed strategy with recall >= target AND cost <= adaptive cost.
    dominators = [r for r in fixed
                  if r["recall"] >= RECALL_TARGET and r["cost"] <= adaptive["cost"]]
    assert not dominators, \
        f"a fixed strategy dominates the optimizer: {[d['name'] for d in dominators]}"
    # 3. Sanity: at least one fixed strategy fails the target (proving no single
    #    fixed choice trivially wins the whole mixed workload).
    assert any(r["recall"] < RECALL_TARGET for r in fixed), \
        "expected some fixed strategy to fail the recall target on a mixed workload"

    print("SELF-CHECK PASSED: the adaptive optimizer is the cheapest strategy "
          "meeting the recall target; no fixed strategy dominates it.\n")


if __name__ == "__main__":
    main()
