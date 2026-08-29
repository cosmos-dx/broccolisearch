# PRD.md — Product Requirements Document

**Product:** BroccoliSearch
**Type:** Embeddable search library + optional server (`broccolid`)
**One-liner:** A search engine with a cost-based query optimizer that automatically picks indexes, budgets, retrieval strategy, and ranking across lexical, vector, structured, graph, temporal, and semantic indexes.

---

## 1. Problem statement

Teams building search today face a forced, bad choice:

1. **Use a lexical engine (Elasticsearch):** great keyword search, but semantic/vector search is second-class and "hybrid" is a manual, hand-tuned bolt-on.
2. **Use a vector DB:** great semantic search, but keyword/filter search is weak and "hybrid" is a fixed fusion formula that ignores what each *specific query* actually needs.

In **both** cases, the developer becomes the query optimizer: they hardcode "run BM25, also run vector, RRF-merge them, filter by status." That hardcoded strategy is wrong for a large fraction of queries — a pure-keyword query (`error code E-4021`) wastes latency on a vector search; a conceptual query (`ways to reduce cloud spend`) is under-served by BM25; a heavily-filtered query should filter *first* and search a tiny survivor set.

**Nobody has a cost-based optimizer that decides this per query.** That is the product.

---

## 2. Vision

> Developers `index()` their data and `search()` with intent. BroccoliSearch decides *how* to answer each query — which indexes, how many candidates, what ranking — the way a SQL database decides how to run a query. It gets faster over time by learning from its own query history, and it can always tell you *why* it chose a plan.

---

## 3. Target users

| Persona | Need | Why BroccoliSearch |
|---|---|---|
| **Backend/platform engineer** | Add strong search to a product without becoming an IR expert. | One API; the engine handles hybrid strategy and tuning. |
| **AI/RAG engineer** | High-recall retrieval for LLM context, with filters + freshness. | Native hybrid + structured + temporal, measured recall. |
| **Search/IR researcher** | A testbed for retrieval strategies and optimizer policies. | Pluggable indexes + a query planner with an evaluation harness. |
| **Data/observability engineer** | Fast filtered search over large structured+text corpora. | Bitmap-first filtering + lexical + optional vector. |

---

## 4. Use cases

1. **Product search** — keyword + semantic + structured filters (price, category, in-stock), ranked well, sub-100ms.
2. **RAG retrieval** — fetch the top-k most relevant passages for an LLM, with metadata filters and recency bias, optimizing recall@k.
3. **Log/document search** — heavy structured filtering + keyword, occasional semantic.
4. **Knowledge base / support search** — paraphrase-tolerant semantic search with exact-match fallback for codes/IDs.
5. **Research experiments** — swap indexes/rankers/planner policies and measure on BEIR/MS MARCO.

---

## 5. Goals and non-goals

### Goals

- G1. A single API that ingests documents with text, vectors, and structured fields.
- G2. A **cost-based optimizer** that chooses the execution plan per query.
- G3. First-class **hybrid** retrieval (lexical + vector + structured) with strong defaults (RRF).
- G4. **Explainability:** every query can return the chosen plan and why (`explain=true`).
- G5. **Measurability:** built-in evaluation harness for recall@k / nDCG / latency-at-fixed-recall.
- G6. **Rust core, Python bindings** — fast engine, easy experimentation.
- G7. Excellent, minimal, "beautiful" API (see §8).

### Non-goals (v1)

- N1. Not a distributed system on day one (single-node first; architecture leaves room — see SystemDesign.md).
- N2. Not a Kibana-style UI (headless library + server; visualization is out of scope).
- N3. Not an embedding-model provider (we consume embeddings; we don't train models).
- N4. Not a general-purpose OLAP/SQL database.
- N5. Graph and temporal axes are *designed* but not required for v1 to prove the thesis.

> These non-goals are about **focus**, not permanence. The architecture is designed so each can be added without a rewrite.

---

## 6. Functional requirements

### 6.1 Indexing

- FR1. Create/open an index directory (`.broccoli`).
- FR2. Add/update/delete documents. A document = `{ id, text fields, vector field(s), structured fields }`.
- FR3. Configurable analyzers per text field (tokenizer, lowercase, stemming, stopwords).
- FR4. Configurable vector fields (dimension, metric, ANN params).
- FR5. Structured fields typed (keyword, int, float, bool, datetime) and bitmap/columnar-indexed.
- FR6. Immutable-segment writes with background merge; near-real-time visibility of new docs.
- FR7. Batch ingest path for bulk loading.

### 6.2 Querying

- FR8. A unified query object expressing: text intent, vector/semantic intent, structured filters, time constraints, `k`, and optional recall/latency targets.
- FR9. The optimizer selects indexes, candidate budgets, order, and ranker automatically.
- FR10. Manual override: caller may pin a strategy (force lexical/vector/hybrid, set budgets) for debugging/benchmarking.
- FR11. `explain=true` returns the chosen plan, estimated vs. actual costs, and per-stage counts.
- FR12. Pagination / top-k with stable ordering.

### 6.3 Ranking

- FR13. Pluggable rankers: BM25, vector similarity, RRF fusion, weighted fusion, optional cross-encoder reranker hook.
- FR14. Configurable fusion defaults; per-query overrideable.

### 6.4 Optimizer

- FR15. Rule-based cost model (v1): estimates operator cost from index statistics + query features.
- FR16. Plan enumeration + selection meeting a recall target at minimum estimated latency.
- FR17. Query-history logging (plan, estimate, actual) to enable learned policies later.
- FR18. Pluggable policy interface so a learned planner can replace/augment rules without API change.

### 6.5 Operations / interfaces

- FR19. Embeddable Rust library (`broccoli-core`).
- FR20. Python package (`broccoli`) via PyO3.
- FR21. Optional server `broccolid` with a JSON/gRPC API.
- FR22. Evaluation harness CLI: run a labeled dataset, emit recall/nDCG/latency report.

---

## 7. Non-functional requirements

- NFR1. **Latency:** p95 < 100ms for hybrid search on a 1–10M doc single-node corpus (target, to be validated).
- NFR2. **Recall:** optimizer must not lose recall vs. the best fixed strategy at equal or lower latency (the core promise).
- NFR3. **Memory:** vector index memory documented and bounded; mmap for lexical/structured to keep RSS predictable.
- NFR4. **Durability:** crash-safe writes; no data loss on process kill (WAL or atomic segment commit).
- NFR5. **Portability:** Linux + macOS; x86-64 + arm64; SIMD used where available with scalar fallback.
- NFR6. **Determinism:** identical inputs → identical results (except where ANN approximation is explicitly in play, which is bounded and reported).
- NFR7. **Observability:** structured metrics per stage (candidates in/out, latency, index touched).

---

## 8. The API (the "beautiful API" requirement)

Illustrative target ergonomics (not final signatures). Python:

```python
import broccoli

idx = broccoli.Index.create("./products.broccoli", schema={
    "title":    broccoli.Text(analyzer="english"),
    "body":     broccoli.Text(analyzer="english"),
    "embedding": broccoli.Vector(dim=768, metric="cosine"),
    "price":    broccoli.Float(),
    "status":   broccoli.Keyword(),
    "created":  broccoli.Datetime(),
})

idx.add({"id": "p1", "title": "Broccoli seeds",
         "embedding": embed("organic broccoli seeds"),
         "price": 4.99, "status": "active", "created": "2026-01-01"})

# The caller expresses INTENT. The optimizer decides HOW.
results = idx.search(
    text="organic green vegetable seeds",
    semantic="healthy garden vegetables",
    where={"status": "active", "price": broccoli.lt(10)},
    recent="30d",
    k=20,
    explain=True,          # get the chosen plan back
)

print(results.plan)        # e.g. filter(bitmap) → ann(ef=64) ⨝ bm25 → rrf → top20
for hit in results:
    print(hit.id, hit.score)
```

Rust core mirrors this with typed builders. The design rule: **the caller states intent + constraints; the engine owns strategy.**

---

## 9. Success metrics

| Metric | Definition | Target |
|---|---|---|
| **Optimizer win rate** | % of query classes where adaptive plan beats the best *single fixed* strategy on latency-at-fixed-recall | > 60% of classes, net positive overall |
| **Recall@100** | on BEIR/MS MARCO subsets | ≥ best fixed hybrid strategy |
| **p95 latency** | hybrid search, 1–10M docs | < 100ms |
| **Explainability** | every query returns a correct, human-readable plan | 100% |
| **Developer time-to-first-search** | fresh install → indexed + searching | < 15 min |

The headline scientific/product claim we must be able to demonstrate: **the adaptive optimizer beats any single hardcoded strategy on latency-at-fixed-recall.** If that's true, the product and the paper both exist.

---

## 10. Risks (product-level)

- **The optimizer's win is marginal.** Mitigation: pick datasets/workloads with mixed query types where per-query decisions clearly matter; report per-class wins, not just aggregate.
- **Scope explosion.** Mitigation: the six axes are a *vision*; v1 ships three (lexical/vector/structured). See SHAPE.md no-gos.
- **Reinventing index engines.** Mitigation: rent Tantivy/usearch/roaring; forbid rewriting them.
- **Cold-start for learned planner.** Mitigation: rule-based core stands alone; learning is an override that needs history.

See **Research.md** for the scientific risks and **SHAPE.md** for scope control.
