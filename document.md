# BroccoliSearch — Master Document

> **The one-line thesis:** A search engine with a **database-style, cost-based query optimizer** that automatically chooses indexes, candidate budgets, retrieval strategy, and ranking pipeline across **lexical, vector, structured, graph, temporal, and semantic** indexes.

This is the entry point to the BroccoliSearch documentation set. Read this first; it tells you what the project is, what each document covers, and the vocabulary used everywhere else.

---

## 1. What is BroccoliSearch?

Most search systems today are one of two shapes:

- **Lexical engines** (Elasticsearch / Lucene): fast keyword/BM25 search with a *rule-based* query planner that only understands text and structured filters.
- **Vector databases** (Qdrant, Weaviate, Milvus, LanceDB): fast approximate-nearest-neighbour (ANN) search over embeddings, with keyword search bolted on and a **fixed** fusion formula for "hybrid" search.

Neither has a real **cost-based optimizer (CBO)** that reasons across *fundamentally different* index types and decides, **per query**, how to answer it: which indexes to touch, in what order, how many candidates to generate, and which ranking pipeline to run.

BroccoliSearch is that missing layer. The individual index engines are treated as **rented, best-in-class dependencies**. The originality lives entirely in the **planner + optimizer + hybrid ranking** layer that sits above them.

```
                 Python API  (embeddings, experimentation, orchestration)
                     │
                     ▼
             ┌──────────────┐
             │  PyO3 / FFI  │
             └──────┬───────┘
                    │
                    ▼
        ┌───────────────────────────┐
        │      Query Optimizer       │   ← the product / the research
        │  (cost model + planner)    │
        └───────────┬───────────────┘
                    │  execution plan
        ┌───────────┼───────────────┬───────────────┐
        ▼           ▼               ▼               ▼
    Inverted     Vector         Bitmap /        Storage
     Index        ANN           Structured       Engine
   (Tantivy)    (usearch)       (roaring)        (mmap)
```

---

## 2. The core beliefs (why this can win)

1. **Rent the indexes, own the planner.** Reimplementing Lucene or FAISS worse than they already exist is a two-year detour that proves nothing. Our contribution is the cross-index cost-based optimizer.
2. **Don't vector-search everything.** Vector search costs you on every query. Filter cheaply first (bitmaps), then run ANN over the survivors.
3. **Candidate generation → ranking.** A cheap high-recall stage narrows millions to hundreds; an expensive high-precision stage reranks the hundreds. This staging is non-negotiable.
4. **Cost is a curve, not a scalar.** For a vector index "cost" is a *recall vs. latency tradeoff*, not a row count. Unifying cost across deterministic (lexical) and approximate (vector) indexes is the hard, novel part.
5. **Rule-based first, learned later.** A hand-tuned cost model is debuggable and gets ~80% of the value. "Learn the optimal plan from query history" is a real feature, but it is an *override* on top of the rule-based core, not the foundation.
6. **Measure everything.** We never ship a retrieval change we cannot score on a labeled dataset (recall@k, nDCG, latency-at-fixed-recall).

---

## 3. The document set

| Document | Purpose | Read it when you want to… |
|---|---|---|
| **[document.md](./document.md)** | This master index + vocabulary. | Understand the whole thing in 10 minutes. |
| **[Information.md](./Information.md)** | Domain knowledge base: how search actually works (inverted index, BM25, embeddings, ANN/HNSW, bitmaps, IR metrics, ELK). | Learn/refresh the fundamentals the design assumes. |
| **[PRD.md](./PRD.md)** | Product requirements: vision, users, use cases, features, API surface, success metrics, non-goals. | Know *what* we are building and for whom. |
| **[Architecture.md](./Architecture.md)** | Component/crate architecture, module boundaries, data flow, the Rust-core + Python-bindings split. | Understand *how the pieces fit together*. |
| **[SystemDesign.md](./SystemDesign.md)** | Deep design: storage engine, indexing pipeline, query execution, the optimizer internals, concurrency, memory, distribution. | Implement or review a subsystem. |
| **[Approach.md](./Approach.md)** | Engineering methodology and principles: how we build, decision rules, what to reuse, evaluation discipline. | Decide *how* to make a technical decision. |
| **[Research.md](./Research.md)** | The research angle: the CBO thesis, related work, novelty, evaluation methodology, datasets, open problems. | Write the paper / defend the novelty. |
| **[SHAPE.md](./SHAPE.md)** | Shape Up shaping doc: problem, appetite, solution sketch, rabbit holes, no-gos. | Scope the work and avoid feature-sprawl. |

Reading order for a newcomer: **document → Information → PRD → Architecture → SystemDesign → Approach → Research → SHAPE**.

Three programs turn the claims above into numbers, and any of them can contradict the docs — the third already does:

| Run | Answers |
|---|---|
| `python3 examples/demo.py` | Does the optimizer actually beat every fixed strategy on work-at-fixed-recall? |
| `python3 examples/cost_model_error.py` | How wrong are the cost estimates, per query shape and per chosen plan? |
| `python3 examples/beir_eval.py --data ./scifact` | On **real judged data**, is the quality competitive with published baselines? |

The BEIR run is the one that matters most, because synthetic ground truth can only show the system agrees with itself. It confirmed the retrieval engines are correct (our BM25 scores 0.664 nDCG@10 on SciFact against a published 0.665) and simultaneously **falsified the optimizer's objective**: it optimizes for operator fidelity, not relevance, so it never selects the fusion plan that wins on every real dataset tested. See [Research.md](./Research.md) §4 (H1) and §7.3.

---

## 4. Glossary (canonical vocabulary)

Terms below are used identically across all documents.

- **Index (BroccoliSearch sense):** a specialized data structure that can produce candidate document IDs and/or scores for a query. Six axes: **lexical, vector, structured/bitmap, graph, temporal, semantic**.
- **Inverted index:** term → posting list of documents; the core of lexical search.
- **Posting list:** the sorted list of document IDs (and positions/frequencies) for a term.
- **BM25:** the standard lexical relevance scoring function.
- **Embedding:** a dense float vector representing the meaning of text/image/etc.
- **ANN (Approximate Nearest Neighbour):** finding the closest vectors *approximately* but fast. **HNSW** and **IVF** are the two dominant algorithms.
- **HNSW:** Hierarchical Navigable Small World graph — a graph-based ANN index.
- **Bitmap index:** a compressed bitset per attribute value; makes structured filters (`status = active`) nearly free. We use **roaring bitmaps**.
- **Candidate generation:** cheap, high-recall stage that produces a candidate set.
- **Ranking / reranking:** expensive, high-precision stage that orders candidates.
- **Hybrid ranking / fusion:** combining scores from multiple indexes (e.g. **RRF** — Reciprocal Rank Fusion — as the strong baseline; learned fusion later).
- **Cost model:** estimates the (latency, recall) cost of an operator or plan.
- **Calibration:** measuring the cost model's constants on the machine and corpus actually in use, rather than hardcoding them. Fitted from timed samples as `latency = base + work × slope` (SystemDesign.md §6.4.1).
- **Work units:** a deterministic count of algorithmic effort (postings scanned, distance computations). Preferred over wall-clock when comparing plans, because at sub-millisecond scale interpreter and scheduler noise exceeds the difference between plans.
- **Query plan / execution plan:** the concrete sequence of index operations chosen by the optimizer to answer a query.
- **Optimizer / planner (CBO):** the component that enumerates candidate plans and picks the cheapest one meeting the recall target.
- **Candidate budget:** how many candidates a stage is allowed to produce (a tunable knob the optimizer sets).
- **Segment:** an immutable unit of on-disk index data (Lucene/Tantivy concept we adopt).
- **Recall@k / nDCG / MRR:** IR quality metrics (defined in Information.md).

---

## 5. Scope guardrails (the short version)

**In scope (v1 core):** single-node engine; lexical + vector + bitmap indexes; candidate→rank pipeline; rule-based cost-based optimizer; RRF hybrid fusion; Rust core with Python bindings; measurement harness on a labeled dataset.

**Explicitly deferred (fully designed, not built first):** distributed/sharded execution, graph & temporal index axes, the *learned* adaptive planner, multi-tenancy, and a hosted service. These are described in full in the design docs so the architecture accommodates them — but they are not the thing that proves the thesis.

> The BEIR results promoted one of these from "deferred" to "blocking", and then complicated it. The learned planner was deferred on the assumption that rules were good enough; real judged data showed the rules optimize the wrong objective (operator fidelity, not relevance), so `LearnedPolicy` was built. Measured on held-out queries it **loses to the rules** — not because the mechanism is wrong but because 150 judged queries per split cannot resolve the 0.02–0.08 nDCG differences it needs to learn. See [README](./README.md) and [Research.md](./Research.md) §4 (H4). The list above is therefore now: learned planner *built and honestly negative*; everything else still deferred.

See **[SHAPE.md](./SHAPE.md)** for the hard no-gos and rabbit holes.

---

## 6. Naming conventions

| Thing | Name |
|---|---|
| Project | **BroccoliSearch** |
| Rust core crate | `broccoli-core` |
| Rust optimizer crate | `broccoli-optimizer` |
| Python package (PyO3 bindings) | `broccoli` |
| Server / daemon | `broccolid` |
| CLI | `broccoli` (subcommands) |
| On-disk format | `.broccoli` index directory |

> Note: the file you may have expected as `Appraoch.md` is created with the corrected spelling **`Approach.md`**.
