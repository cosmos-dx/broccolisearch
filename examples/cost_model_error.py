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
K = (10, 50, 200)                # marshalling is O(k), so k must vary
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
    """Query shapes that exercise every operator and both vector modes.

    `k` varies deliberately. Result marshalling is O(k), so a workload with a
    single fixed k cannot tell a per-hit cost apart from a constant and scores
    that whole term as correct no matter what it is.
    """
    def vec(c):
        return list(centroids[c] + rng.normal(scale=0.05, size=DIM))

    cases = []
    for i, c in enumerate(range(0, N_CONCEPTS, 40)):
        k = K[i % len(K)]
        cases.append(("keyword", k, {"text": f"topic{c}"}))
        cases.append(("semantic", k, {"semantic": vec(c)}))
        cases.append(("hybrid", k, {"text": f"topic{c}", "semantic": vec(c)}))
        cases.append(("filtered", k, {"semantic": vec(c),
                                      "where": {"category": CATEGORIES[c % 5]}}))
        cases.append(("filtered_kw", k, {"text": f"topic{c}",
                                         "where": {"category": CATEGORIES[c % 5]}}))
        cases.append(("range", k, {"semantic": vec(c),
                                   "where": {"price": broccoli.lt(10)}}))
    return cases


def measure(idx, cases):
    """One full pass: {shape: [errors]}, {plan: [errors]}, [all errors]."""
    by_shape, by_plan, all_errors = defaultdict(list), defaultdict(list), []
    for shape, k, kwargs in cases:
        for _ in range(WARMUP):
            idx.search(k=k, **kwargs)
        samples = []
        for _ in range(REPEATS):
            started = time.perf_counter()
            idx.search(k=k, **kwargs)
            samples.append((time.perf_counter() - started) * 1000.0)
        actual = min(samples)

        explained = idx.search(k=k, explain=True, **kwargs).explain
        error = abs(explained.plan.estimate.latency_ms - actual) / max(actual, 1e-9)
        by_shape[shape].append(error)
        by_plan[explained.plan.name].append(error)
        all_errors.append(error)
    return by_shape, by_plan, all_errors


def _table(title, grouped):
    print("-" * 44)
    print(f"{title:<16}{'n':>4}{'median err':>13}{'mean err':>11}")
    print("-" * 44)
    for key in sorted(grouped):
        errs = grouped[key]
        print(f"{key:<16}{len(errs):>4}{statistics.median(errs):>12.1%}"
              f"{sum(errs) / len(errs):>11.1%}")


def main(builds: int = 3):
    """Report error across INDEPENDENT calibrations, not just one.

    A single build reports one draw from a distribution: on identical code this
    harness produced anywhere from 9% to 40% depending only on which constants
    that run's calibration happened to fit. Quoting one number was therefore
    reporting calibration luck as model accuracy, and it made every attempted
    improvement unfalsifiable. The spread across builds is the honest headline.
    """
    per_build = []
    by_shape_all, by_plan_all = defaultdict(list), defaultdict(list)
    for _ in range(builds):
        rng = np.random.default_rng(11)
        idx, centroids = build(rng)
        cases = workload(centroids, rng)
        by_shape, by_plan, errors = measure(idx, cases)
        per_build.append(statistics.median(errors))
        for key, errs in by_shape.items():
            by_shape_all[key] += errs
        for key, errs in by_plan.items():
            by_plan_all[key] += errs

    n_cases = sum(len(v) for v in by_shape_all.values()) // builds
    print(f"\ncost-model error, {n_cases} queries x {REPEATS} repeats "
          f"x {builds} independent calibrations "
          f"({N_CONCEPTS * DOCS_PER_CONCEPT} docs)\n")
    _table("query shape", by_shape_all)
    _table("chosen plan", by_plan_all)
    print("-" * 44)
    pooled = [e for errs in by_shape_all.values() for e in errs]
    print(f"{'OVERALL':<16}{len(pooled):>4}{statistics.median(pooled):>12.1%}"
          f"{sum(pooled) / len(pooled):>11.1%}")
    print(f"\nper-calibration medians: "
          f"{', '.join(f'{m:.1%}' for m in per_build)}")
    print("The spread across those is calibration variance, not model error; "
          "a fix only\ncounts if it moves the whole set (Approach.md §7).\n")
    return statistics.median(pooled)


if __name__ == "__main__":
    main()
