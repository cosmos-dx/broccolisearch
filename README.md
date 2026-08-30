# BroccoliSearch

> A search engine with a **database-style, cost-based query optimizer** that automatically chooses indexes, candidate budgets, retrieval strategy, and ranking pipeline across **lexical, vector, structured, graph, temporal, and semantic** indexes.

Most search stacks force you to hand-code and hand-tune a retrieval strategy ("run BM25 + vector, RRF-merge, filter by status"), and that fixed strategy is wrong for a large fraction of real queries. **BroccoliSearch makes the engine the query optimizer** — you express *intent*, it decides *how* to answer each query, the way a SQL database plans a query.

The novelty isn't the indexes (we rent `hnswlib` for ANN). It's the **optimizer** that reasons across them per query. See [Research.md](./Research.md).

---

## Install

```bash
pip install -e .            # or: pip install -r requirements.txt
```

Requires Python ≥3.9 and `numpy`. `hnswlib` is optional — without it the vector index still returns **exact** results, just exhaustively.

### Optional: the Rust core

The lexical scan has a native implementation. It is **optional**: without it the
pure-Python path runs and every test still passes.

```bash
cd broccoli-core && maturin build --release && pip install --force-reinstall target/wheels/*.whl
```

It speeds up the posting-list scan and cuts the cost model's error on keyword
queries from 43.5% to 8.9%. Set `BROCCOLI_NO_RUST=1` to force the Python path —
that is how the test suite runs both and asserts they agree. See
[The Rust core](#the-rust-core) below.

## Quickstart

```python
import broccoli

idx = broccoli.Index.create("./products.broccoli", schema={
    "title":     broccoli.Text(analyzer="english"),
    "embedding": broccoli.Vector(dim=768, metric="cosine"),
    "price":     broccoli.Float(),
    "status":    broccoli.Keyword(),
})

idx.add({"id": "p1", "title": "Organic broccoli seeds",
         "embedding": embed("organic broccoli seeds"),
         "price": 4.99, "status": "active"})
idx.commit()                       # calibrates the cost model, then persists

# You state INTENT and CONSTRAINTS. The optimizer picks the plan.
results = idx.search(
    text="organic seeds",
    semantic=embed("healthy garden vegetables"),
    where={"status": "active", "price": broccoli.lt(10)},
    k=20,
    recall=0.9,                    # a target, not a strategy
    explain=True,
)

print(results.plan)   # filter(kept=812) -> vector(domain=812, ef=16, n=100) | ...
for hit in results:
    print(hit.id, hit.score)
```

`explain=True` returns the chosen plan, the plans it considered, estimated vs. actual cost, and per-stage candidate counts.

---

## Does the optimizer actually work?

Measured on a 50k-document mixed workload (keyword / semantic / filtered queries), `python3 examples/demo.py`:

```
strategy                 recall     nDCG     MRR        work    target
----------------------------------------------------------------------
ADAPTIVE (optimizer)      1.000    1.000   1.000        9607      yes
ADAPTIVE (learned)        1.000    1.000   1.000        8200      yes
lexical                   0.400    0.451   0.667        3377       NO
vector                    1.000    1.000   1.000        9583      yes
hybrid_rrf                1.000    1.000   1.000        9607      yes
```

The learned optimizer matches the best fixed strategy's recall using **1.17× less work** (8200 vs 9583), *without being told which strategy to use* — it routes to `lexical` on the queries whose terms are selective enough to answer alone, and to `vector` on the rest. `lexical` alone is cheapest but fails the recall bar; the two vector strategies hit recall but overpay on queries that never needed a vector search.

The two adaptive rows are the interesting part, and they say something the earlier version of this README got wrong:

- **`ADAPTIVE (optimizer)`** has no judgments. It can measure that its two indexes return different documents, but not whether that difference *matters* — so it hedges and fuses, spending 9607.
- **`ADAPTIVE (learned)`** is trained on half the judged queries and scored on the other half. It measures that on this corpus the lexical index alone is perfect for one class of query and useless for another, and routes accordingly for 8200.

That gap is the honest price of not having relevance labels, and it is why `Policy` is a swappable interface rather than a fixed rule.

**Honest caveats**, because the project's own rules require a measured number with its limits stated:

- The win is **1.17×, not dramatic**, and it needs judgments. Filter push-down happens during planning and therefore benefits *every* strategy including the fixed ones, so this workload understates what the optimizer would save in a system where fixed strategies don't get that for free.
- **Work units, not wall-clock, are the trustworthy metric.** At this scale in Python, latency swings ~15% with run *order* alone (cold caches) — larger than the gap between plans. The latency columns are indicative only.
- The cost model's estimate error is **~17% median** (`examples/cost_model_error.py`), concentrated in sub-0.05ms queries where fixed per-query cost is most of the total. It is calibrated for **ranking plans correctly**, which the tests assert, not for predicting absolute latency. See the error section below — the previously advertised "10–15%" was a measurement artifact, not a better model.
- This particular workload is synthetic. See below for what happened on real judged data — it is not the same story.

### Fixing the cost model was also a speedup

Making the estimates accurate was not just bookkeeping — a wrong cost model was making wrong plans. Measured on the 60-query mixed workload against a clean checkout, the whole run went from **23.1ms to 10.1ms (2.3× faster)**:

| Fix | Effect |
|---|---|
| Exact-vs-ANN chosen by **comparing costs** instead of a hardcoded `EXACT_SCAN_MAX = 2048` | filtered queries **3–5× faster** (the threshold kept picking the slower path) |
| Structured filter hands the planner a **raw id set** instead of a `{id: 1.0}` score map it converts back to a set | two whole-domain allocations per filtered query, gone |
| `top_k` is a **bounded heap** (`O(n log k)`) rather than a full sort | a filter-only query no longer sorts the corpus to return 10 hits |
| Lexical scan iterates **whichever of posting list and filter domain is smaller** | selective filters stop dragging long posting lists through the interpreter |
| Vector domain lookup **vectorised**; `VectorIndex.n_docs` no longer rebuilds a set of every id per call | planning went from **27× the cost of execution** to a fraction of it |
| `fit_linear` fits with **Theil–Sen** instead of least squares | calibrated constants repeat within ±5%; under OLS they varied by up to four orders of magnitude between identical runs |

Two of those were found only because the estimates were wrong in a specific, traceable way. A filter's survivors were being counted as candidates to rank, so every filtered plan was priced as if it would rank the whole surviving corpus — the cost model was biased against the exact push-down it exists to exploit. And the reported error itself was inflated by comparing an estimate that includes planning cost against a measured time that excludes it.

### The error measurement was lying, and fixing it mattered more than any model change

This README previously advertised **~10–15% median** cost-model error. That number was not reproducible. Running the unchanged harness on unchanged code produced 12.7%, 24.4%, 27.4% and 40.5% on four consecutive runs — the quoted figure was the luckiest draw from a wide distribution, and every "improvement" measured against it was unfalsifiable.

The variance was not timing noise. Holding calibration fixed and re-measuring three times gave 20.0%, 21.4%, 22.5% — stable. Re-*calibrating* the identical corpus moved the error by 6+ points and swung `pipeline_ms` by 2x. **Calibration was the variable, not the model.** Three real bugs came out of that:

| Bug | Effect |
|---|---|
| `ranking.calibrate` timed fusion and top-k **once each**, with no warm-up and no min-of-N — the only calibrator in the library that did | its constants are charged against every candidate a hybrid plan fuses, so one unlucky sample moved a hybrid estimate by tens of percent. Fixing it took overall error **35.9% → 21.2%** and hybrid **50.1% → 19.4%** |
| `pipeline_ms` was computed as `min(total − execution)` per run, subtracting two nearly-equal noisy timers and keeping the unluckiest pair | minimising each quantity separately removed most of the 2x swing |
| The pipeline probe walked k up to 100 using an arbitrary term that matched only 50 documents, so the curve flattened where it should have risen | the fit flipped between "all fixed cost" and "all per-hit cost" run to run. Probing with the most common term identified the slope consistently |

Two modelling errors were fixed alongside them: result marshalling is O(k) and was priced as a constant, and it is O(*hits returned*) rather than O(k), so a `k=200` query against a 50-document term was being charged four times over.

The harness itself now reports across **independent calibrations** and varies `k` across the workload, because a fixed-k workload cannot distinguish a per-hit cost from a constant and scores that entire term as correct no matter what it is.

Current honest number: **17.4% median**, reproducible to within ~2 points across independent calibrations (16.4%, 17.7%, 18.9%). It remains worst on sub-0.05ms keyword queries, where fixed overhead is nearly the whole latency, and best on vector plans (9.3%).

The earlier standalone simulation (`experiments/thesis_prototype.py`, no dependencies) shows a larger 1.81× on an idealized cost model — the gap between the two is exactly why the real library was measured separately.

---

## On real judged data (BEIR)

Synthetic ground truth can only prove the system is self-consistent, so the same harness was run against BEIR — real corpora, real queries, human relevance judgments, `all-MiniLM-L6-v2` embeddings — using `examples/beir_eval.py`.

**The retrieval engines are correct.** Our BM25 lands on the published BEIR baseline almost exactly, which is the check that would have caught an analyzer or scoring bug:

| Dataset | our BM25 nDCG@10 | published BEIR BM25 |
|---|---|---|
| SciFact (5,183 docs / 300 queries) | **0.664** | 0.665 |
| NFCorpus (3,633 docs / 323 queries) | **0.318** | 0.325 |

**Real data exposed a defect in the optimizer's objective, which is now fixed.** Originally the optimizer scored 0.647 nDCG on SciFact while `hybrid_rrf` scored 0.693, and it *never chose fusion at any recall target*. That was not a tuning problem but a definitional one:

> The cost model's `recall` meant **operator fidelity** — did the ANN return the true nearest neighbours — not **relevance**. An exact vector scan honestly reports 1.0, so no plan could ever outrank it, even though fusing with BM25 retrieves *different* relevant documents that vector search alone never sees.

The fix is to measure what one index misses. At calibration time, `Index._measure_index_agreement` samples documents as queries, runs the fusion, and records what fraction of the fused top-k each index would have returned alone. No relevance judgments are needed — the indexes' own disagreement supplies the signal. Measured coverage is ~0.67 per index on SciFact and ~0.57 on NFCorpus, so a single-index plan is now correctly priced as *incomplete* and fusion can win.

The result is that `recall` became a real dial instead of an inert argument. On SciFact:

```
recall=   nDCG@10    work   plan mix
  0.30     0.6751     842   lexical=2  vector=188
  0.50     0.6751     842   lexical=2  vector=188
  0.70     0.6910    4617   hybrid_rrf=190
  0.90     0.6910    4617   hybrid_rrf=190
```

Below the measured coverage the optimizer buys the cheap single-index plan; above it, it pays for fusion. Before the fix this curve was flat — every target returned the vector plan. At the default `recall=0.9` the optimizer now matches `hybrid_rrf` exactly (0.691 on SciFact, 0.313 on NFCorpus), so **no quality is left on the table**.

What it does *not* buy is a free lunch. Reaching fusion's quality costs fusion's work, because on a homogeneous workload there is nothing to route — every SciFact query is the same shape. Per-query routing is what H1 predicts a win from, and it needs a *mixed* workload, which these datasets are not.

### Cost differences below the model's own error must not decide plans

An early version of the rule-based policy would price two plans at 850 and 851
work units and take the 0.1% saving — discarding a plan that was dramatically
better on quality for a saving far below the cost model's own ~17% median error.
Discriminating between plans on a difference you cannot measure is choosing on
noise, so `RuleBasedPolicy` now prefers the plan that consults **more evidence**
when two are indistinguishably cheap (`COST_TIE_BAND` in
`broccoli/optimizer.py`, with tests in `tests/test_broccoli.py`). SciFact is
unchanged at 0.693 nDCG and the `recall` dial still traces its curve, because
there fusion costs 5× more and never enters the tie band.

---

### The learned policy: built, and it finds the best value point

`LearnedPolicy` (`broccoli/optimizer.py`) runs each plan shape over judged training queries, records the nDCG each actually achieved bucketed by the **fraction of the corpus** the rarest query term matches, and at query time picks the **cheapest plan not measurably worse than the best** — where "measurably" means the gap survives both a tolerance and two standard errors of the *paired* per-query difference. Pairing matters: query difficulty dominates nDCG variance and is common to both plans, so it cancels in the difference and makes gaps detectable on a few hundred queries that are invisible in either plan's absolute mean.

Trained on half of each dataset and scored on the held-out half:

| | scifact nDCG@10 | work | | nfcorpus nDCG@10 | work |
|---|---|---|---|---|---|
| ADAPTIVE (rules) | 0.691 | 4617 | | 0.313 | 1479 |
| **ADAPTIVE (learned)** | **0.675** | **842** | | **0.297** | **646** |
| lexical | 0.649 | 3782 | | 0.282 | 641 |
| vector | 0.673 | 850 | | 0.291 | 850 |
| hybrid_rrf | 0.691 | 4617 | | 0.313 | 1479 |

On SciFact the learned policy **dominates the vector baseline outright** — better nDCG (0.675 vs 0.673) for less work (842 vs 850) — and reaches 97.7% of fusion's quality for **5.5× less work**. It gets there by learning per bucket that, for example, queries whose rarest term matches under 0.1% of the corpus are answered as well by lexical alone as by fusion, while queries in the 0.1–1% band genuinely need fusing.

Getting here took three failed attempts, which is worth recording because two of them looked reasonable:

1. **Learning recall@k** — picks fusion everywhere. Fusion retrieves strictly more relevant documents, so a recall-maximising policy always fuses and pays 5x for it.
2. **Learning nDCG with absolute-count buckets** — lost to the rules (0.658 vs 0.675). A bucket boundary of "50 documents" means *selective* in a 5k corpus and *common* in a 50k one, so on larger corpora every query collapsed into one bucket and there was nothing left to route on. Switching to corpus **fraction** fixed it.
3. **Learning unpaired means** — the differences being learned (0.02–0.08 nDCG) are the same size as their standard error at ~150 training queries, so the estimates did not transfer across splits.

The remaining honest limit: it still gives up ~0.016 nDCG to full fusion, and it needs judgments. The next real test is MS MARCO, where judged queries are ~1000x more plentiful and the buckets would not be data-starved.

Reproduce it:

```bash
curl -sLO https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip && unzip -q scifact.zip
pip install sentence-transformers          # only needed for this script
PYTHONPATH=. python3 examples/beir_eval.py --data ./scifact
```

---

## The Rust core

```bash
cd broccoli-core && maturin build --release && pip install --force-reinstall target/wheels/*.whl
```

**Only the lexical scan was ported, and that is a result rather than an
omission.** The vector index is a single numpy matmul that already runs in BLAS
with SIMD; replacing a tuned kernel with a hand-written Rust loop is a good way
to lose a benchmark. The scan is where the interpreter was actually standing in
the way — a Python `dict` update per posting, several million times a second.

The gain is largest on long posting lists and disappears on short ones, which is
Amdahl's law rather than a disappointment: a query whose posting list holds a
single entry is almost entirely Python pipeline overhead, and making one stage
free cannot speed a query up by more than that stage's share of it.

### It also answered an open question about the cost model

The remaining question was whether the cost model's large error on cheap
keyword queries was a modelling failure or an artefact of timing sub-0.05ms
operations in Python. Running the same instrument over both backends, nine
independent calibrations each, settles it:

| query shape | Python | Rust |
|---|---|---|
| keyword | 43.5% | **8.9%** |
| filtered | 18.4% | 7.4% |
| semantic | 17.7% | 17.5% |
| filtered_kw | 24.7% | 33.0% |
| **overall** | **24.8%** | **18.5%** |

So it was **largely a Python floor**. The model was predicting a scan whose cost
was dominated by interpreter overhead it could not see; the same model over a
native scan is 5x more accurate on exactly the queries it was worst at. The
`semantic` row is the control — nothing about the vector path changed, and its
error did not move.

Calibration variance dropped too, which matters because
[Approach.md](./Approach.md) shows it was the dominant term: the per-calibration
medians span 6.9%–40.3% on Python but only 9.8%–29.7% on Rust. More predictable
execution makes the *instrument* more repeatable, not just the engine faster.

`filtered_kw` is the one row that got **worse**, and it is an honest residual —
see the FFI cost below.

### What the port cost, and the bug it caused

The first working version made filtered keyword queries **80x more expensive
than the cost model predicted** (`filtered_kw` error 23.7% → 68.9%). Passing the
filter's surviving-document set across the FFI boundary costs O(|domain|)
*whatever Rust then does with it*, and the cost model charges only
`min(df, |domain|)`. Pushing a 4,000-document filter onto a 50-document posting
list therefore paid 4,000 units of invisible marshalling to save nothing.

The fix is to decide the join order **before** crossing the boundary rather than
inside Rust: when the posting lists are the smaller side, they are scanned
unfiltered and the non-members are dropped in Python, so the domain is never
marshalled at all. That restored `filtered_kw` to 27–33% and left the model
charging the quantity the code actually spends.

This is the general lesson of the port, and it is not "Rust is faster": *a
faster operator is only useful if the cost model still describes it.* An
optimizer that mispredicts its own fastest operator will route queries away from
it.

### Guarantee: the two backends agree exactly

The port is an implementation swap, not a behaviour change, and that is
asserted rather than assumed. `test_rust_core_and_python_agree_exactly` runs
both over the same corpus and compares scores with `==`, not `approx` — BM25
sums float contributions per query term, so the backends only agree bit-for-bit
if they accumulate in the same order at the same width. A tolerance is exactly
where a real scoring divergence would hide.

The test was checked by mutation: perturbing Rust's `B` constant by 1e-10 makes
it fail. That check exists because the first version of the test passed
*vacuously* — it queried `"gardening"` against an index that had stemmed the
word to `"garden"`, so both backends returned nothing and agreed perfectly about
it.

Analysis deliberately stays in Python. It runs once per document rather than
once per posting, so it is not hot, and a second stemmer implementation would
eventually drift from the first — index-time and query-time analysis disagreeing
is the classic silent way to destroy recall.

---

## What's implemented

| Area | Status |
|---|---|
| Schema + validation (text, vector, keyword, int, float, bool, datetime) | done |
| Lexical index: analyzer, inverted index, posting lists, BM25 | done |
| Vector index: HNSW via `hnswlib`, exact numpy path, filtered ANN | done |
| Structured index: bitmaps, sorted-column ranges, selectivity stats | done |
| **Cost-based optimizer**: featurize → enumerate → estimate → policy | done |
| Cost as a **(latency, recall) pair**; ANN budget rides a measured curve | done |
| Filter push-down (flips vector search to exact when survivors are few) | done |
| Ranking: BM25, cosine, **RRF**, weighted fusion, reranker hook | done |
| `explain`: chosen plan, alternatives, estimated vs. actual, stage stats | done |
| Calibration of every index against the real machine | done |
| Query history logging (the training signal for a learned policy) | done |
| Evaluation harness: recall@k, nDCG, MRR, work/latency-at-fixed-recall | done |
| BEIR runner on real judged data (`examples/beir_eval.py`) | done |
| Persistence: save/open | done |
| `LearnedPolicy`: measured relevance per query bucket, paired significance test | done |
| Relevance-aware cost model: measured per-index coverage of a fused answer | done |
| **Rust core (PyO3): native inverted index + BM25 scan, optional, bit-identical** | done |
| Rust ports of the vector and structured engines | not built — see below |
| Graph/temporal axes, distribution | designed, not built |

71 tests, and they run twice: `python3 -m pytest tests/ -q` and
`BROCCOLI_NO_RUST=1 python3 -m pytest tests/ -q`.

> **On the rest of the Rust core:** [Architecture.md §6](./Architecture.md)
> specifies native engines behind the `BaseIndex` interface, and the lexical one
> now exists — which is the part that proves the interface holds, since the
> optimizer, the calibration and the whole test suite were unchanged by the
> swap. The vector engine is deliberately still numpy (BLAS already beats what a
> hand-written Rust loop would do), and renting Tantivy / usearch / roaring
> instead of the hand-rolled structures remains future work.

---

## Layout

```
broccoli/
├── engine.py        # the public Index facade (create/open/add/search)
├── optimizer.py     # THE POINT: featurize, enumerate, cost model, Policy
├── execution.py     # runs a plan, honours budgets, emits stage stats
├── indexes/
│   ├── lexical.py       # analyzer + inverted index + BM25
│   ├── vector.py        # HNSW + exact path + calibrated recall curve
│   └── structured.py    # bitmaps + sorted-column ranges
├── ranking.py       # RRF, weighted fusion, recency decay
├── calibration.py   # robust (Theil-Sen) fit of base + marginal cost
├── stats.py         # statistics + query history
├── eval.py          # judged-query harness and IR metrics
├── query.py         # Query/filters/Plan/Explain
└── schema.py        # field types + validation

broccoli-core/           # optional Rust extension (PyO3)
└── src/lib.rs           # native inverted index + BM25 scan
```

## Documentation

Start with **[document.md](./document.md)** (master index + glossary).

| Doc | Covers |
|---|---|
| [document.md](./document.md) | Master index, thesis, glossary. |
| [Information.md](./Information.md) | ELK, inverted index/BM25, embeddings, ANN/HNSW, bitmaps, IR metrics. |
| [PRD.md](./PRD.md) | Requirements, users, use cases, API, success metrics. |
| [Architecture.md](./Architecture.md) | Crates/modules, the `Index`/`Policy` traits, data flow. |
| [SystemDesign.md](./SystemDesign.md) | Storage, optimizer internals, concurrency, distribution. |
| [Approach.md](./Approach.md) | Reuse ladder, decision rules, evaluation discipline. |
| [Research.md](./Research.md) | The CBO thesis, prior art, hypotheses, open problems. |
| [SHAPE.md](./SHAPE.md) | Appetite, rabbit holes, hard no-gos. |

## Next steps

- [x] Run the harness on judged data (BEIR) instead of synthetic ground truth — done, and it found a real defect in the optimizer's objective.
- [x] **Teach the cost model that relevance ≠ operator fidelity** — done via measured per-index coverage; `recall` is now a working dial.
- [x] `LearnedPolicy` — built, and now the best value point on both BEIR datasets.
- [ ] Train the policy on **MS MARCO** (~500k judged queries rather than 300), where the buckets would not be data-starved.
- [ ] **Validate filter push-down on real data.** It is the mechanism the design leans on hardest, and it is still only exercised synthetically — neither BEIR corpus ships usable structured fields. This needs a corpus with real metadata to filter on.
- [ ] **Test H1 on a mixed workload with real judgments.** H1 predicts a per-query routing win only where query *shapes* vary; SciFact and NFCorpus are homogeneous, so there is nothing to route and the claim stays untested on real data.
- [x] **Settle the sub-0.05ms error** — done, and it was largely a Python floor: keyword-query error fell 43.5% → 8.9% under the native scan, with the vector path unchanged as a control.
- [x] **Port the hot operator to Rust behind the same interface** — done; `broccoli-core` speeds up the scan with bit-identical results and no interface change. See [Architecture.md §6.3](./Architecture.md) for why not Go.
- [ ] Model the FFI marshalling cost so `filtered_kw` stops being the worst row (33.0%): the estimate charges `min(df, |domain|)` but the boundary crossing is real work the model cannot see.
- [ ] Rent Tantivy / usearch / roaring in place of the hand-rolled structures, now that the interface has survived one real swap.

## License

MIT.
