# SHAPE.md — Shaping Document

A Shape Up-style shaping of BroccoliSearch: the problem, the appetite, a solution sketch at the right altitude, the rabbit holes to avoid, and the hard no-gos. This is the scope-control document — when in doubt about whether to build something, this doc decides.

> Note on "full-fledged, not phases": the *design* across all docs is complete and end-to-end. This SHAPE doc is where we protect that design from turning into an infinite build. Designing everything ≠ building everything at once.

---

## 1. Problem

Developers adding search must hand-code a retrieval strategy (run BM25 + vector, RRF-merge, filter by status) and then hand-tune it. That fixed strategy is wrong for a large fraction of real queries:

- A code/ID query (`error E-4021`) wastes latency on vector search it doesn't need.
- A conceptual query (`ways to cut cloud cost`) is under-served by keyword-only.
- A heavily-filtered query should filter first and search a tiny survivor set, but usually doesn't.

**The developer is forced to be the query optimizer, and does it badly.** Databases solved this 40 years ago with cost-based optimizers. Search never got one that spans lexical *and* vector *and* structured indexes.

---

## 2. Appetite

- **Core thesis experiment:** small (weeks). This is the bet — prove an adaptive optimizer beats fixed strategies on latency-at-fixed-recall.
- **Usable single-node engine (v1):** medium (a focused effort, not open-ended). Lexical + vector + structured, real optimizer, real API, measured.
- **Everything else** (graph/temporal axes, learned planner, distribution, server hardening, UI): **not in this appetite.** Fully designed in the docs, built only after the core earns it.

If a piece of work doesn't fit the appetite, we cut scope (narrow it), we don't extend the appetite.

---

## 3. Solution sketch (the right altitude — not a spec)

```
        Query (intent: text? semantic? filter? recency? k? recall target)
                              │
                    ┌─────────▼──────────┐
                    │  Cost-based planner │   featurize → enumerate → estimate → choose
                    └─────────┬──────────┘
                              │  plan (indexes + budgets + fusion + ranker)
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                       ▼
  structured filter     candidate gen           ranking / fusion
  (roaring bitmap)   (Tantivy / usearch,      (BM25 + cosine → RRF,
   shrink universe    budget-bounded, over        optional rerank)
   FIRST)             the survivor set)
                              │
                           top-k  (+ explain: the chosen plan)
```

The fat marker version of the bet: **the planner box in the middle.** Everything above and below it is rented parts wired together.

### The core "fat marker" flows

- **Filtered semantic:** `bitmap filter → ANN over survivors → BM25 rerank → RRF → top-k`
- **Pure keyword (rare terms):** `lexical BM25 → top-k` (planner skips vector entirely)
- **Conceptual:** `ANN(ef tuned to recall target) → optional lexical union → RRF → top-k`

The planner *chooses among these*, per query, from cheap features — that choosing is the product.

---

## 4. Rabbit holes (identified, and how we avoid them)

| Rabbit hole | Why it's dangerous | How we avoid it |
|---|---|---|
| **Rebuilding an index engine** | Two years reimplementing Lucene/FAISS worse; proves nothing. | Hard no-go (§6). Tantivy/usearch/roaring only. |
| **Unified scalar cost model** | Wrong by construction (approximate ops have curves). Endless tweaking. | Model cost as (latency, recall); calibrate curves offline (SystemDesign §6.4). Accept bounded error. |
| **Learned planner too early** | No history to learn from; unfalsifiable; masks a broken cost model. | Rules first; learning is a drop-in `Policy` after rules are a proven ceiling. |
| **Distribution** | 10x bug surface; irrelevant to the single-node thesis. | Designed (SystemDesign §12), not built in appetite. |
| **Six index axes** | Graph/temporal/semantic are huge; dilute the bet. | v1 = three axes (lexical/vector/structured). Others slot into the `Index` trait later. |
| **Perfect API before proof** | Bikeshedding ergonomics before the engine is worth using. | Freeze a "good enough beautiful" API (PRD §8); iterate after the thesis holds. |
| **Cross-encoder reranking latency** | Big precision gains, big latency cost; easy to over-rely on. | Optional hook, off by default; measured as an explicit plan choice. |
| **Embedding model ownership** | We are not an ML shop; training/serving models is a separate universe. | Consume embeddings; never produce them. |

---

## 5. What's in scope (v1)

- Single-node embeddable engine (`broccoli-core`) + Python bindings (`broccoli`).
- Three index axes: **lexical (Tantivy), vector (usearch/HNSW), structured (roaring)**.
- Storage: immutable segments, mmap, WAL + atomic commit, background merge.
- **Rule-based cost-based optimizer** with filter push-down, budget selection on calibrated ANN curves, and `explain`.
- Ranking: BM25, vector similarity, **RRF** (default), weighted fusion; optional reranker hook.
- **Evaluation harness** (`broccoli-eval`) on BEIR/MS MARCO: recall@k, nDCG, latency-at-fixed-recall.
- Query-history logging (wires the loop for a future learned policy — logged, not yet learned).

---

## 6. No-gos (hard boundaries — do not cross without re-shaping)

1. **No reimplementing** inverted index, HNSW, or bitmap internals. Rent them.
2. **No learned optimizer** until the rule-based one is working and demonstrated to be a ceiling.
3. **No distribution** until single-node correctness + the thesis are proven.
4. **No graph/temporal axis code** in v1 (design only).
5. **No UI / dashboard** (headless library + optional server only).
6. **No embedding-model training/serving.**
7. **No retrieval change without a measured recall/latency number.**
8. **No abstraction without a current requirement using it.**

---

## 7. The bet, restated

We are betting that **a per-query, cost-based optimizer across lexical + vector + structured indexes beats any single fixed strategy on latency-at-fixed-recall**, and that this is both a publishable result and a product wedge that removes the "developer-as-query-planner" burden.

We de-risk the bet by running the **minimal falsifying experiment first** (Research.md §8): one judged dataset, three strategies, a dumb feature-keyed picker. If the picker wins, we scale it into the v1 engine. If it doesn't, we found out in weeks — which is the entire reason to shape it this way.

---

## 8. Circuit breakers (when to stop and re-shape)

- The minimal experiment shows **no** win over fixed hybrid → re-examine query features / datasets before building more.
- Cost-model **estimate-vs-actual error** stays large after calibration → the cost model is the project; fix it before adding plans.
- Planning overhead approaches query cost → bound plan enumeration harder; simplify the plan space.
- Scope pressure to add an axis/distribution/UI → return to §6 no-gos; the answer is "designed, not now."
