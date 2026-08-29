# Approach.md — Engineering Methodology & Principles

This document is about *how we make decisions* while building BroccoliSearch — not a phased schedule. It's the decision framework, the reuse discipline, and the evaluation rigor that keep the project honest and small.

---

## 1. The prime directive

> **Rent the indexes. Own the planner.**

Every hour spent reimplementing an inverted index or an HNSW graph is an hour not spent on the only thing that's novel: the cross-index cost-based optimizer. If you catch yourself writing posting-list compression or SIMD distance kernels, stop — that already exists and is better than what you'll write.

---

## 2. The reuse ladder (apply before writing any code)

For every capability, stop at the first rung that holds:

1. **Does it need to exist at all?** (YAGNI — is this feature required to prove the thesis?)
2. **Is it already in this codebase?** Reuse the helper/pattern.
3. **Does the standard library do it?** Use it.
4. **Does a native platform feature cover it?** (mmap, page cache, `rename` atomicity.)
5. **Does an installed dependency solve it?** (Tantivy, usearch, roaring, rayon, PyO3.)
6. **Can it be one line?** Make it one line.
7. **Only then** write the minimum code that works.

Worked examples:

| Need | Rung reached | Decision |
|---|---|---|
| Inverted index / BM25 | 5 | Tantivy. Never rebuild. |
| ANN / HNSW | 5 | usearch (or FAISS bindings). Never rebuild. |
| Bitmap index | 5 | roaring. Never rebuild. |
| Atomic commit | 4 | `write-temp + rename` + WAL. Platform gives atomic rename. |
| Parallel per-segment search | 5 | rayon. |
| Score fusion baseline | 6 | RRF is ~10 lines. |
| **Cost-based optimizer** | 7 | **This is the code we write.** Nothing else does it. |

The pattern: the ladder pushes *everything* to a dependency **except** the optimizer. That's intentional — it's the signal that we're spending originality where it counts.

---

## 3. What we are NOT lazy about

Laziness = efficiency, not carelessness. We do *not* cut corners on:

- **Understanding the problem first.** Read the task, trace the real flow (index → optimize → execute → rank), *then* pick a rung. A small diff in the wrong place is a second bug.
- **Trust boundaries.** Validate documents/queries at ingest and API edges (dimension mismatch, oversized fields, malformed filters).
- **Durability.** Crash-safety (WAL + atomic manifest) is non-negotiable; losing indexed data is unacceptable.
- **Measurement.** No retrieval change ships without a recall/nDCG/latency number on a labeled set.
- **Calibration to real behavior.** ANN recall/latency curves are dataset-dependent; measure them, don't assume the spec-ideal. Three rules, each learned the hard way (SystemDesign.md §6.4.1):
  - **Calibrate the operator, not the kernel.** Time what execution runs end to end, including result marshalling — not the inner `knn_query` or dot product.
  - **Calibrate on the access pattern you execute.** Real filters yield scattered ids; timing a contiguous prefix measures a memory gather that never happens in production.
  - **Fit robustly.** Timing noise is one-sided — a process can only be interrupted and made slower — so least squares chases outliers. Use a median-based (Theil–Sen) fit over min-of-N samples. This is not a micro-optimization: under OLS, recalibrating an unchanged corpus moved constants by four orders of magnitude, which moved plan choice for reasons unrelated to the query.
- **Constants are measured once, not learned online.** A cost term updated from live traffic couples queries together: query N's plan silently depends on query N−1's latency, and results stop being reproducible. Calibrate at build time; leave adaptation to an explicit `LearnedPolicy`.
- **Explainability.** The optimizer must always be able to say *why* it chose a plan.

Non-trivial logic leaves **one runnable check** behind — the smallest thing that fails if the logic breaks (an assert-based self-check or a tiny test), no frameworks, no fixtures. Trivial one-liners need none.

---

## 4. Decision rules (fast heuristics for common forks)

- **Rust vs. Go for a component:** Rust for anything in the data/query path (memory, SIMD, mmap, dependency ecosystem). The only Go-shaped work here would be peripheral tooling, and even that we keep in Rust for one toolchain.
- **New dependency vs. write it:** prefer a mature, widely-used dependency over bespoke code — *unless* it's the optimizer (rung 7). Avoid a new dep if an installed one already covers it.
- **Abstraction:** none that weren't explicitly requested. The `Index`/`Policy` traits exist because the optimizer genuinely needs polymorphism and the learned-planner swap — not for hypothetical futures.
- **Rule vs. learned:** always rules first. Learned only after there's history *and* the rules are demonstrably a ceiling.
- **Feature vs. focus:** if a feature (graph axis, distribution, UI) isn't needed to prove "adaptive beats fixed strategy," it's deferred — designed in the docs, not coded.
- **Two stdlib approaches, same size:** pick the edge-case-correct one. Lazy means less code, not the flimsier algorithm.

---

## 5. Marking simplifications

Every intentional shortcut gets a `ponytail:` comment. If it has a known ceiling, the comment names the ceiling *and* the upgrade path. Examples already in the design:

- Global single-writer per index → ceiling: write throughput → upgrade: per-shard writers.
- Global ANN cost curve (not per-query-type) → ceiling: mixed workloads → upgrade: per-cluster/learned curves.
- Rule-based policy → ceiling: threshold generalization → upgrade: LearnedPolicy on logged history.

This keeps the shortcuts *visible and intentional*, so the next person (or you in three months) knows it was a choice, not an accident.

---

## 6. Bug-fixing discipline

A bug report names a **symptom**; we fix the **root cause**:

1. Reproduce with the smallest query/dataset.
2. Trace to the shared function, not the caller that happened to surface it.
3. `grep` every caller of that function — fix the shared function once (one guard there beats one per caller and doesn't leave a sibling caller broken).
4. Add the one runnable check that would have caught it.

For a search engine specifically: a "wrong results" bug is usually in **analysis mismatch** (index-time vs. query-time analyzer differ), **fusion/normalization**, or **budget too small** (recall lost at candidate stage). Check those three before suspecting the index engines themselves.

---

## 7. Evaluation discipline (the thing that makes this science, not vibes)

- **Always measure quality *with* latency.** A latency number without a recall number is meaningless (you can be infinitely fast and wrong).
- **North-star metric:** latency-at-fixed-recall. That's what the optimizer is supposed to improve.
- **Report per query class**, not just aggregate — the optimizer's whole value is making *different* decisions for different query types; an aggregate can hide that.
- **Labeled datasets only** for quality claims (BEIR / MS MARCO). Synthetic timing is fine for latency micro-benchmarks.
- **Regression guard:** the eval harness runs on a fixed dataset subset; a retrieval change that drops recall or nDCG without a latency justification is rejected.
- **Estimate-vs-actual tracking:** the optimizer logs predicted vs. real cost. A large gap is a cost-model bug and is treated as a first-class defect (a mis-calibrated optimizer makes bad plans confidently). `examples/cost_model_error.py` is the instrument; it reports error per query shape and per chosen plan, because an aggregate hides which operator is wrong.
- **Qualify the instrument before trusting it.** Run the measurement several times unchanged and look at the spread first. Ours swung 18%→81% run to run, which meant it could not resolve the differences we were reacting to — we were tuning noise. Fixing the *instrument* (and the calibration variance behind it) was worth more than any estimate-formula change.
- **Report across the thing that varies, not one draw from it.** Even after the above, the harness quoted a single build and produced 12.7%, 24.4%, 27.4% and 40.5% on consecutive unchanged runs; the README advertised the best of those. Bisecting it (fix the calibration, re-measure → stable to ±1.3 points; re-calibrate, re-measure → 6+ points of movement) named calibration as the variable and led directly to three real bugs. The harness now reports across independent calibrations.
- **A benchmark that holds a parameter fixed cannot grade a term that depends on it.** Every query in the error workload used `k=50`, so an O(k) marshalling cost was indistinguishable from a constant and scored as correct no matter what it held. Vary the parameter or drop the term.
- **When a self-check fails after a change you believe in, read it before weakening it.** Making the cost model relevance-aware broke the demo's dominance assertion. The check was right: a judgment-free coverage measure cannot tell that two *different* result sets are equally relevant, so on a corpus of interchangeable documents it over-prices fusion. That reframed the demo into its real subject — what judgments buy — rather than being tuned away.
- **Score the model against the statistic it was calibrated on.** Calibration times operators with `min()`, so the model predicts interference-free cost; grading it against a median charges it for scheduler noise it deliberately excluded.
- **Compare like with like.** The estimate models the whole query; grade it against whole-query latency. We spent a while chasing a "60% error" that was really the estimate including planning cost while the measured actual excluded it — the model was right and the comparison was wrong.
- **Confirm profiler findings with wall-clock before believing them.** `cProfile` adds per-call overhead, so it systematically overstates exactly the Python-level call and loop overhead you are trying to remove. A change that looked like 2.15× under the profiler was ~0% in wall-clock; the wins that *were* real showed up in both. Profile to find *where* to look, measure wall-clock to decide whether it mattered.
- **Benchmark against a clean checkout, not memory.** A throwaway `git worktree` at the pre-change commit gives a true baseline with zero risk to the working tree, and it catches the case where the thing you "already fixed" was never actually the bottleneck.

---

## 8. Definition of done (for any unit of work)

A change is done when:

1. It does the minimum that works (rung 7 or lower).
2. Intentional simplifications carry `ponytail:` comments with ceilings/upgrade paths.
3. Non-trivial logic has its one runnable check.
4. If it touches retrieval, it has a recall/nDCG/latency number from the harness.
5. Trust-boundary inputs are validated; durability isn't compromised.
6. If it touches the optimizer, `explain` still produces a correct plan and estimate-vs-actual is logged.

---

## 9. Anti-goals (things that would mean we lost the plot)

- Rebuilding Lucene/FAISS/roaring "because we can."
- A learned optimizer before a working rule-based one.
- Distribution before single-node correctness.
- Six index axes before three of them are measured to help.
- A UI before the engine has a defensible quality/latency story.
- Abstractions "for flexibility" that no current requirement uses.

If a task smells like one of these, re-read §1 and §4.
