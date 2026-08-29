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
- The cost model's estimate error is **~20% median** (`examples/cost_model_error.py`), down from ~85%, and what remains is concentrated in sub-0.05ms operations where interpreter noise dominates. It is calibrated for **ranking plans correctly**, which the tests assert, not for predicting absolute latency.
- The workload is synthetic. Running this on judged data (BEIR / MS MARCO) is the next real step.

### Fixing the cost model was also a speedup

Making the estimates accurate was not just bookkeeping — a wrong cost model was making wrong plans:

| Fix | Effect |
|---|---|
| Exact-vs-ANN chosen by **comparing costs** instead of a hardcoded `EXACT_SCAN_MAX = 2048` | filtered queries **3–5× faster** (the threshold kept picking the slower path) |
| `VectorIndex.n_docs` no longer rebuilds a set of every id on each call | planning **6.4× cheaper**; it had been costing more than execution |
| `fit_linear` fits with **Theil–Sen** instead of least squares | calibrated constants repeat within ±5%; under OLS they varied by up to four orders of magnitude between identical runs |

The last one was the root cause. Before it, re-running the *same* measurement on *unchanged* code gave anywhere from 18% to 81% error — the model wasn't systematically wrong, it was randomly wrong, so every per-operator "fix" measured before it was tuning noise.

The earlier standalone simulation (`experiments/thesis_prototype.py`, no dependencies) shows a larger 1.81× on an idealized cost model — the gap between the two is exactly why the real library was measured separately.

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
| Persistence: save/open | done |
| Learned policy, graph/temporal axes, distribution, Rust core | designed, not built |

56 tests: `python3 -m pytest tests/ -q`

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

- [ ] Run the harness on judged data (BEIR / MS MARCO) instead of synthetic ground truth.
- [ ] `LearnedPolicy` trained on the query history already being logged.
- [ ] Port the engines to Rust behind the same interfaces (Tantivy / usearch / roaring).

## License

MIT.
