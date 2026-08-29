#!/usr/bin/env python3
"""End-to-end demo: does the optimizer actually beat fixed strategies?

This is the thesis experiment (Research.md §8) re-run against the REAL library
instead of the standalone simulation in experiments/thesis_prototype.py.

    PYTHONPATH=. python3 examples/demo.py

Workload design matters here, so it is deliberate:

* Every query carries BOTH a text and a semantic intent, so every fixed
  strategy is genuinely applicable to every query. Otherwise a strategy would
  score zero for being inapplicable rather than for being wrong, and the
  comparison would be rigged.
* Relevant sets are sized comparably to k, so recall@k can actually reach 1.0.
  Judging recall@20 against a 250-document relevant set would cap every
  strategy at 0.08 and tell us nothing.
* The three query shapes favour three different strategies, so no single fixed
  choice can win the workload. That is the whole hypothesis.

Ends in an assert-based self-check.
"""

from __future__ import annotations

import time

import numpy as np

import broccoli

N_CONCEPTS = 1000
DOCS_PER_CONCEPT = 50           # 50k documents, 50-doc relevant sets
DIM = 64
CATEGORIES = ["tools", "seeds", "books", "pots", "soil"]
N_SYNONYMS = 5
K = 50
RECALL_TARGET = 0.9


def build_index(rng):
    centroids = rng.normal(size=(N_CONCEPTS, DIM))
    idx = broccoli.Index.create(schema={
        "title": broccoli.Text(analyzer="english"),
        "body": broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=DIM, metric="cosine"),
        "price": broccoli.Float(),
        "category": broccoli.Keyword(),
    })

    total = N_CONCEPTS * DOCS_PER_CONCEPT
    print(f"indexing {total} documents...", flush=True)
    started = time.perf_counter()
    for c in range(N_CONCEPTS):
        for i in range(DOCS_PER_CONCEPT):
            # Every doc of a concept carries the canonical term, so a keyword
            # query is precise. Only 1/N_SYNONYMS carry any given synonym, so a
            # synonym query has a vocabulary gap only meaning can bridge.
            idx.add({
                "id": f"c{c}d{i}",
                "title": f"topic{c} syn{c}x{i % N_SYNONYMS} gardening item{i}",
                "body": f"a description of topic{c} for the garden, unit {i}",
                "embedding": list(centroids[c] + rng.normal(scale=0.35, size=DIM)),
                "price": float(i),
                "category": CATEGORIES[i % len(CATEGORIES)],
            })
    print(f"  indexed in {time.perf_counter() - started:.1f}s")

    print("calibrating cost model on this machine...", flush=True)
    started = time.perf_counter()
    idx.calibrate()
    print(f"  calibrated in {time.perf_counter() - started:.1f}s")
    print("  measured ANN recall curve: "
          + str({ef: round(p["recall"], 3) for ef, p in idx.vector.curve.items()}))
    return idx, centroids


def build_workload(centroids, rng):
    """Three query shapes; no fixed strategy is best at all three."""
    judgments = []

    def vec(c):
        return list(centroids[c] + rng.normal(scale=0.05, size=DIM))

    # 1. KEYWORD intent — the canonical term matches every relevant doc, so
    #    lexical is both precise and cheap. Vector would also work, but costs
    #    more. The optimizer should prefer lexical.
    for c in range(0, N_CONCEPTS, 25):
        relevant = {f"c{c}d{i}": 1.0 for i in range(DOCS_PER_CONCEPT)}
        judgments.append(broccoli.Judgment(
            query={"text": f"topic{c}", "semantic": vec(c)}, relevant=relevant))

    # 2. SEMANTIC intent — the caller's word only appears in 1/N_SYNONYMS of the
    #    relevant docs, so lexical alone cannot reach them. Vector should win.
    for c in range(1, N_CONCEPTS, 25):
        relevant = {f"c{c}d{i}": 1.0 for i in range(DOCS_PER_CONCEPT)}
        judgments.append(broccoli.Judgment(
            query={"text": f"syn{c}x0", "semantic": vec(c)}, relevant=relevant))

    # 3. FILTERED semantic — a selective filter should be pushed down first,
    #    which shrinks the domain enough to make the vector search exact.
    for c in range(2, N_CONCEPTS, 25):
        cat = CATEGORIES[c % len(CATEGORIES)]
        relevant = {f"c{c}d{i}": 1.0 for i in range(DOCS_PER_CONCEPT)
                    if CATEGORIES[i % len(CATEGORIES)] == cat}
        judgments.append(broccoli.Judgment(
            query={"text": f"syn{c}x0", "semantic": vec(c),
                   "where": {"category": cat}}, relevant=relevant))

    return judgments


def main():
    rng = np.random.default_rng(42)
    idx, centroids = build_index(rng)
    judgments = build_workload(centroids, rng)

    train, test = judgments[::2], judgments[1::2]

    harness = broccoli.Harness(idx, test, k=K, recall_target=RECALL_TARGET)
    reports = harness.compare()
    learned = broccoli.LearnedPolicy().fit(idx, train, k=K)
    previous, idx.optimizer.policy = idx.optimizer.policy, learned
    reports.insert(1, harness.run_strategy("ADAPTIVE (learned)", None))
    idx.optimizer.policy = previous

    print(harness.report(reports=reports))
    print(learned.describe())

    adaptive = next(r for r in reports if r.name == "ADAPTIVE (learned)")
    hedged = next(r for r in reports if r.name.startswith("ADAPTIVE (optim"))
    fixed = [r for r in reports if not r.name.startswith("ADAPTIVE")]
    print(f"\nrule-based (no judgments) hedges into fusion: {hedged.work:.0f} work "
          f"units at recall {hedged.recall:.3f}\nlearned (judged) routes per query "
          f"instead: {adaptive.work:.0f} work units at recall {adaptive.recall:.3f}")

    print("NOTE: work units are the trustworthy cost metric here. Wall-clock at "
          "this\n      scale swings ~15% with run ORDER alone (cold caches), "
          "which is larger\n      than the gap between plans, so the latency "
          "columns are indicative only.\n")

    print("plans the optimizer chose across the workload:")
    for plan, count in sorted(idx.statistics()["plans"].items(),
                              key=lambda kv: -kv[1]):
        print(f"  {plan:<16} {count}")
    print(f"\nmean cost-model error: {idx.statistics()['mean_estimate_error']:.1%}")

    # ---- self-check ----
    # Judged on WORK UNITS, which are deterministic. Wall-clock differences
    # between plans here are smaller than the run-to-run noise of a Python
    # implementation, so asserting on latency would test the scheduler.
    dominators = [r for r in fixed
                  if r.recall >= adaptive.recall - 1e-9
                  and r.work <= adaptive.work * 0.999]
    assert not dominators, \
        f"a fixed strategy dominates the optimizer: {[d.name for d in dominators]}"
    assert adaptive.recall >= max(r.recall for r in fixed) - 0.02, \
        (f"optimizer gave up too much recall: {adaptive.recall:.3f} vs best fixed "
         f"{max(r.recall for r in fixed):.3f}")
    print("\nSELF-CHECK PASSED: given judgments, no fixed strategy matches the "
          "optimizer's\nrecall at lower cost.\n")


if __name__ == "__main__":
    main()
