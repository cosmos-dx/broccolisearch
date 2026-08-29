#!/usr/bin/env python3
"""Measure how wrong the cost model is, per plan and per operator.

    PYTHONPATH=. python3 examples/cost_model_error.py

A confidently-wrong optimizer picks bad plans, so estimate-vs-actual error is a
first-class defect (Approach.md §7). This is the instrument that makes the
error objective and repeatable instead of anecdotal.

Method: estimates are deterministic, actual latency is not, so each query is run
`REPEATS` times and compared against the MINIMUM observed latency. Minimum, not
median, because the index calibration routines themselves time with `min()` —
the model therefore predicts interference-free cost, and scoring it against a
median would charge it for scheduler noise it deliberately excluded. Comparing
against a single sample would mostly measure the OS scheduler.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict

import numpy as np

import broccoli

N_CONCEPTS = 400
DOCS_PER_CONCEPT = 50           # 20k docs
DIM = 64
CATEGORIES = ["tools", "seeds", "books", "pots", "soil"]
K = 50
REPEATS = 41
WARMUP = 10


def build(rng):
    centroids = rng.normal(size=(N_CONCEPTS, DIM))
    idx = broccoli.Index.create(schema={
        "title": broccoli.Text(analyzer="english"),
        "body": broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=DIM, metric="cosine"),
        "price": broccoli.Float(),
        "category": broccoli.Keyword(),
    })
    for c in range(N_CONCEPTS):
        for i in range(DOCS_PER_CONCEPT):
            idx.add({
                "id": f"c{c}d{i}",
                "title": f"topic{c} syn{c}x{i % 5} gardening item{i}",
                "body": f"a description of topic{c} for the garden, unit {i}",
                "embedding": list(centroids[c] + rng.normal(scale=0.35, size=DIM)),
                "price": float(i),
                "category": CATEGORIES[i % len(CATEGORIES)],
            })
    idx.calibrate()
    return idx, centroids


def workload(centroids, rng):
    """Query shapes that exercise every operator and both vector modes."""
    def vec(c):
        return list(centroids[c] + rng.normal(scale=0.05, size=DIM))

    cases = []
    for c in range(0, N_CONCEPTS, 40):
        cases.append(("keyword", {"text": f"topic{c}"}))
        cases.append(("semantic", {"semantic": vec(c)}))
        cases.append(("hybrid", {"text": f"topic{c}", "semantic": vec(c)}))
        cases.append(("filtered", {"semantic": vec(c),
                                   "where": {"category": CATEGORIES[c % 5]}}))
        cases.append(("filtered_kw", {"text": f"topic{c}",
                                      "where": {"category": CATEGORIES[c % 5]}}))
        cases.append(("range", {"semantic": vec(c),
                                "where": {"price": broccoli.lt(10)}}))
    return cases


def main():
    rng = np.random.default_rng(11)
    idx, centroids = build(rng)
    cases = workload(centroids, rng)

    by_shape = defaultdict(list)
    by_plan = defaultdict(list)
    all_errors = []

    for shape, kwargs in cases:
        for _ in range(WARMUP):
            idx.search(k=K, **kwargs)
        samples = []
        for _ in range(REPEATS):
            started = time.perf_counter()
            idx.search(k=K, **kwargs)
            samples.append((time.perf_counter() - started) * 1000.0)
        actual = min(samples)

        explained = idx.search(k=K, explain=True, **kwargs).explain
        estimate = explained.plan.estimate.latency_ms
        error = abs(estimate - actual) / max(actual, 1e-9)
        by_shape[shape].append(error)
        by_plan[explained.plan.name].append(error)
        all_errors.append(error)

    print(f"\ncost-model error, {len(cases)} queries x {REPEATS} repeats "
          f"({N_CONCEPTS * DOCS_PER_CONCEPT} docs)\n")
    print(f"{'query shape':<16}{'n':>4}{'median err':>13}{'mean err':>11}")
    print("-" * 44)
    for shape in sorted(by_shape):
        errs = by_shape[shape]
        print(f"{shape:<16}{len(errs):>4}{statistics.median(errs):>12.1%}"
              f"{sum(errs) / len(errs):>11.1%}")
    print("-" * 44)
    print(f"{'chosen plan':<16}{'n':>4}{'median err':>13}{'mean err':>11}")
    print("-" * 44)
    for plan in sorted(by_plan):
        errs = by_plan[plan]
        print(f"{plan:<16}{len(errs):>4}{statistics.median(errs):>12.1%}"
              f"{sum(errs) / len(errs):>11.1%}")
    print("-" * 44)
    print(f"{'OVERALL':<16}{len(all_errors):>4}"
          f"{statistics.median(all_errors):>12.1%}"
          f"{sum(all_errors) / len(all_errors):>11.1%}\n")
    return statistics.median(all_errors)


if __name__ == "__main__":
    main()
