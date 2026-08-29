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
ADAPTIVE (optimizer)      1.000    1.000   1.000        8200      yes
lexical                   0.400    0.451   0.667        3377       NO
vector                    1.000    1.000   1.000        9583      yes
hybrid_rrf                1.000    1.000   1.000        9607      yes

work-at-fixed-recall: adaptive 8200 units vs best fixed (vector) 9583 → 1.17x
```

The optimizer matches the best fixed strategy's recall using **1.17× less work**, and it does so *without being told which strategy to use* — routing to `lexical` for the keyword queries and `vector`/`hybrid_rrf` for the semantic and filtered ones, deterministically. `lexical` alone is cheapest but fails the recall bar; the two vector strategies hit recall but overpay on queries that never needed a vector search.

**Honest caveats**, because the project's own rules require a measured number with its limits stated:

- The win here is **1.17×, not dramatic**. Filter push-down happens during planning and therefore benefits *every* strategy including the fixed ones, so this workload understates what the optimizer would save in a system where fixed strategies don't get that for free.
- **Work units, not wall-clock, are the trustworthy metric.** At this scale in Python, latency swings ~15% with run *order* alone (cold caches) — larger than the gap between plans. The latency columns are indicative only.
- The cost model's estimate error is **~10–15% median** (`examples/cost_model_error.py`), down from ~85%, and what remains is concentrated in sub-0.1ms queries where the fixed per-query cost is most of the total. It is calibrated for **ranking plans correctly**, which the tests assert, not for predicting absolute latency.
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

The calibration fix was the root cause of the rest. Before it, re-running the *same* measurement on *unchanged* code gave anywhere from 18% to 81% error — the model wasn't systematically wrong, it was randomly wrong, so every per-operator "fix" measured before it was tuning noise.

The earlier standalone simulation (`experiments/thesis_prototype.py`, no dependencies) shows a larger 1.81× on an idealized cost model — the gap between the two is exactly why the real library was measured separately.

---

## On real judged data (BEIR)

Synthetic ground truth can only prove the system is self-consistent, so the same harness was run against BEIR — real corpora, real queries, human relevance judgments, `all-MiniLM-L6-v2` embeddings — using `examples/beir_eval.py`.

**The retrieval engines are correct.** Our BM25 lands on the published BEIR baseline almost exactly, which is the check that would have caught an analyzer or scoring bug:

| Dataset | our BM25 nDCG@10 | published BEIR BM25 |
|---|---|---|
| SciFact (5,183 docs / 300 queries) | **0.664** | 0.665 |
| NFCorpus (3,633 docs / 323 queries) | **0.318** | 0.325 |

**The optimizer's objective is wrong, and real data is what exposed it.** On both datasets it picks a cheap plan and leaves relevance on the table:

```
scifact                 nDCG@10     work        nfcorpus            nDCG@10     work
ADAPTIVE (optimizer)      0.647      843        ADAPTIVE              0.319      619
lexical                   0.664     3599        lexical               0.318      600
vector                    0.644      850        vector                0.314      850
hybrid_rrf                0.693     4435        hybrid_rrf            0.346     1439
```

`hybrid_rrf` is the best strategy on both (+0.046 and +0.027 nDCG) and the optimizer **never chooses it**. This is not a tuning problem, it is a definitional one:

> The cost model's `recall` means **operator fidelity** — did the ANN return the true nearest neighbours — not **relevance**. An exact vector scan honestly reports recall 1.0, so no plan can ever outrank it, even though fusing with BM25 retrieves *different* relevant documents that vector search alone never sees.

The synthetic workload hid this because its relevant documents were constructed so that a single index could find all of them; union recall and relevance coincided. On real data they come apart. Estimating what fusion adds requires knowing which index's notion of similarity matches *this* corpus's judgments — which is not derivable from index statistics and is exactly the job of the unbuilt `LearnedPolicy` ([Research.md](./Research.md) §7, open problem 3).

Reproduce it:

```bash
curl -sLO https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip && unzip -q scifact.zip
pip install sentence-transformers          # only needed for this script
PYTHONPATH=. python3 examples/beir_eval.py --data ./scifact
```

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
| Relevance-aware cost model (vs. today's operator-fidelity recall) | **not built — known defect, see BEIR results above** |
| Learned policy, graph/temporal axes, distribution, Rust core | designed, not built |

62 tests: `python3 -m pytest tests/ -q`

> **On the Rust core:** [Architecture.md](./Architecture.md) specifies a Rust engine with PyO3 bindings, which is still the right end state. No Rust toolchain exists in this environment, so this is the Python reference implementation of the same architecture — the `BaseIndex` / `Policy` interfaces are what the optimizer depends on, so each engine can be swapped for Tantivy/usearch/roaring without touching the planner.

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

- [x] Run the harness on judged data (BEIR) instead of synthetic ground truth — done, and it found a real defect in the optimizer's objective (above).
- [ ] **Teach the cost model that relevance ≠ operator fidelity.** This is now the top priority, because it is the one thing standing between the optimizer and the best strategy on every real dataset tested.
- [ ] `LearnedPolicy` trained on the query history already being logged — the BEIR result gives it a concrete job: learn per-index relevance coverage from judgments so fusion can be priced.
- [ ] Port the engines to Rust behind the same interfaces (Tantivy / usearch / roaring).

## License

MIT.
