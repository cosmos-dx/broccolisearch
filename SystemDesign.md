# SystemDesign.md — Deep System Design

This document goes subsystem-by-subsystem into *how each part works internally*. It assumes the layer cake and abstractions from **Architecture.md** and the fundamentals from **Information.md**.

Subsystems covered:

1. Storage engine (segments, mmap, WAL, commit, merge)
2. Schema & document model
3. Indexing pipeline
4. The three index engines (lexical, vector, structured)
5. Statistics store
6. The query optimizer (the heart)
7. The execution engine (operators, budgets)
8. Ranking & fusion
9. Concurrency & memory model
10. Explainability & observability
11. Fault tolerance & durability
12. Distribution (designed, deferred)

---

## 1. Storage engine

### Segments (borrowed from Lucene/Tantivy)

Data is written into **immutable segments**. Immutability buys us: OS page-cache friendliness, lock-free reads, trivial caching, and crash-consistent commits.

```
./products.broccoli/
├── manifest.json          # current committed segment set + schema version
├── wal/                   # write-ahead log for in-flight (uncommitted) writes
├── seg_0001/
│   ├── lexical/           # Tantivy segment files (postings, term dict FST, doc values)
│   ├── vector/            # HNSW graph + vectors (mmap-able)
│   ├── structured/        # roaring bitmaps + columnar values
│   └── docstore/          # stored fields (id, source doc) — block-compressed
├── seg_0002/ ...
└── stats/                 # cardinality stats + query-history log
```

### mmap-first reads

Lexical, structured, and stored-field data are read via **mmap**: the kernel page cache holds hot data, RSS stays predictable, and process restart is cheap (no warm-up copy). The vector HNSW graph is mmap-able but hot; for latency it typically stays resident.

### Commit protocol (crash-safe)

1. Writes append to the **WAL** and a mutable in-memory buffer (visible for near-real-time search).
2. On flush, the buffer is serialized into a new segment directory (written to temp, `fsync`ed).
3. `manifest.json` is atomically swapped (write-temp + `rename`) to include the new segment.
4. The WAL region for those writes is truncated.

A crash before step 3 → replay WAL. A crash during step 3 → the old manifest is intact (rename is atomic). **No torn state.**

### Background merge

Small segments are merged into larger ones to bound segment count (search cost grows with segment count). Merge is a background task producing a new segment + a manifest swap; old segments are deleted after readers drain (see §9).

---

## 2. Schema & document model

A **document** is `{ id, fields }`. The **schema** declares each field's type and index behavior:

| Field type | Indexed into | Notes |
|---|---|---|
| `Text(analyzer)` | Lexical | tokenized/stemmed per analyzer; also stored optionally |
| `Vector(dim, metric, params)` | Vector | one or more per doc; params = HNSW `M`, `ef_construction` |
| `Keyword` | Structured (bitmap) | exact-match, faceting |
| `Int`/`Float` | Structured (columnar + bitmap for ranges) | range queries via sorted columns / BKD-like |
| `Bool` | Structured (bitmap) | trivial bitset |
| `Datetime` | Structured + Temporal | time-partitioned; supports recency decay |

Schema is versioned in `manifest.json`. Schema changes that are additive (new field) are cheap; breaking changes require reindex (documented, not magic).

---

## 3. Indexing pipeline

```
add(doc)
  → validate against schema (trust boundary: reject malformed/oversized)
  → analyze text fields (tokenize/stem)         → lexical buffer
  → accept vector fields (validate dim/metric)  → vector buffer
  → encode structured fields                    → bitmap/columnar buffer
  → store source (id + fields)                  → docstore buffer
  → append to WAL
  → (buffers visible to search immediately)
  → on flush threshold → seal segment (commit protocol §1)
```

- **Batch path:** `add_many()` amortizes analysis + builds ANN graph in bulk (much faster HNSW construction than one-by-one).
- **Deletes:** tombstone bitset per segment; the doc is filtered from results and physically dropped at next merge (standard Lucene approach).
- **Updates:** delete + add (documented; no in-place mutation of immutable segments).

---

## 4. The three index engines

### 4.1 Lexical (Tantivy adapter)

- Provides posting lists, BM25 scoring, term dictionary (FST), positions for phrase queries.
- Exposes to the optimizer: **posting-list lengths → cardinality estimates** (lexical cost is fairly deterministic).
- `estimate()` for a term = f(posting-list length, number of terms, operator AND/OR).

### 4.2 Vector (usearch/HNSW adapter)

- HNSW graph per segment; query merges results across segments.
- **Budget knob = `ef_search`.** `estimate()` returns a point on the **recall/latency curve** for the requested `ef_search`, calibrated from offline measurement of this dataset (see §6.3 — cost calibration).
- Supports pre-filtered ANN: given a bitmap of allowed doc IDs (from structured filter), restrict the graph search to survivors ("filter-then-vector"). This is the mechanism behind "don't vector-search your whole database."

> **Calibration note (`ponytail:`):** the recall/latency curve is dataset-dependent. We measure it once per index build (a handful of sample queries at several `ef_search` values) and store the curve in `broccoli-stats`. Ceiling: the curve is global, not per-query-type; upgrade path is per-cluster or learned curves.

### 4.3 Structured (roaring bitmaps + columnar)

- Equality/boolean filters → roaring bitmap AND/OR/NOT (near-free, SIMD).
- Range filters → sorted columnar values / BKD-tree-like structure → produce a bitmap of matches.
- Output: a **bitmap of candidate doc IDs** that other stages can intersect against — the cheap first stage that shrinks the universe.
- `estimate()` = exact-ish cardinality (bitmaps know their cardinality in O(1)).

---

## 5. Statistics store (`broccoli-stats`)

The optimizer is only as good as its statistics. We keep:

- **Cardinality stats:** per-term posting lengths, per-value bitmap cardinalities, total docs, per-segment counts.
- **Vector cost curves:** measured recall/latency vs. `ef_search` per vector field.
- **Query-history log:** append-only `{query features, chosen plan, estimated cost, actual latency, actual recall (when judgments available)}`. This is (a) observability and (b) the training data for the learned policy.
- **Fusion priors:** which fusion/ranker worked well for which query class (updated online).

Stats are refreshed on commit/merge; curves recalibrated on (re)build.

---

## 6. The query optimizer (`broccoli-optimizer`) — the heart

This is the novel component. It mirrors a SQL cost-based optimizer but over heterogeneous, partly-approximate indexes.

### 6.1 Pipeline

```
featurize → enumerate plans → estimate each → choose (Policy) → emit Plan
```

### 6.2 Query featurization

Cheap features computed from the query + stats, used to decide strategy:

- **Has structured filter?** and its **selectivity** (bitmap cardinality / total). Highly selective filter ⇒ filter-first, tiny survivor set.
- **Lexical specificity:** are query terms rare (high IDF, e.g. codes/IDs) or common? Rare terms ⇒ lexical is cheap and precise.
- **Semantic-ness:** is there a semantic/vector intent? long natural-language query ⇒ vector likely helps.
- **Time constraint / recency:** narrows candidates; may pick temporal partition.
- **k and recall target.**

### 6.3 Plan enumeration

Enumerate a bounded set of candidate plans (not a giant search space — bounded for latency). Examples:

- `P1: lexical(BM25) → top-k` (pure keyword)
- `P2: vector(ef) → top-k` (pure semantic)
- `P3: structured filter → vector(ef over survivors) → BM25 rerank → RRF` (filtered hybrid)
- `P4: lexical ∪ vector → RRF → cross-encoder rerank` (full hybrid)
- `P5: structured filter → lexical` (filtered keyword)

For each, the optimizer also picks **budgets** (candidate counts, `ef_search`) by walking the cost curves to meet the recall target at min latency.

### 6.4 Cost model (the crux)

Each plan's cost is composed from operator estimates. The key design decision: **cost is a (latency, recall) pair, not a scalar.**

```
PlanEstimate = Σ operator_latency,  and  min/product of stage recalls
choose plan = argmin est_latency  subject to  est_recall ≥ target.recall
```

- **Deterministic operators** (lexical, structured): recall ≈ 1 within their capability; latency ≈ f(cardinality).
- **Approximate operators** (vector): recall = curve(ef_search); latency = curve(ef_search). The optimizer *rides the curve* to spend the minimum ef that hits the recall target — and can shrink the vector search domain first via the structured bitmap (cheaper curve because fewer candidates).
- **Filter-first rewrite:** if a selective filter exists, push it down so vector/lexical operate on the survivor bitmap. This is the single biggest latency win and falls straight out of cardinality estimation.

### 6.5 Policy: rule-based now, learned later

- **`RuleBasedPolicy` (v1):** applies the cost model + a small set of hand-tuned thresholds (filter-first when selectivity < X; skip vector when query is all rare terms; etc.). Debuggable, deterministic.
- **`LearnedPolicy` (later):** consumes the query-history log; learns which plan/budget minimizes actual latency-at-fixed-recall per query class. Same `Policy` interface → drop-in. Cold-start falls back to rules.

> **`ponytail:`** v1 does NOT learn. The learned planner is fully designed and the history loop is wired, but we prove the thesis with rules first. Ceiling of rules: hand-tuned thresholds generalize imperfectly; upgrade path = LearnedPolicy on logged history.

### 6.6 Explain

When `explain=true`, the emitted `Plan` carries: chosen steps + budgets, estimated latency/recall, and — after execution — **actual** latency/recall and per-stage candidate counts. This is both a developer feature and our debugging/telemetry backbone.

---

## 7. Execution engine (`broccoli-exec`)

Takes the `Plan` and runs it. Operators are composable and each honors a **budget** and emits **stage stats**.

Operator types:

- `Filter(bitmap)` → produces/《intersects allowed-ID bitmap.
- `LexicalSearch(budget)` → candidates + BM25 scores (optionally restricted to bitmap).
- `VectorSearch(ef, domain?)` → ANN candidates + similarities (optionally restricted to bitmap survivors).
- `Fuse(spec)` → merge candidate sets (RRF/weighted).
- `Rank(spec)` → final scoring / optional cross-encoder rerank.
- `TopK(k)` → bounded heap.

Execution respects the plan's ordering (e.g. filter pushed down before vector). Stage stats (`in`, `out`, `latency`) are recorded for explain + history.

---

## 8. Ranking & fusion (`broccoli-rank`)

- **BM25:** via Tantivy for the lexical component.
- **Vector similarity:** cosine/dot from the ANN stage.
- **RRF (default fusion):** `score(d) = Σ_i 1/(k + rank_i(d))`. No tuning, strong baseline.
- **Weighted fusion:** `α·norm(BM25) + β·norm(cosine)` with score normalization; used when tuned weights exist.
- **Reranker hook:** optional cross-encoder (called via Python/an external model) reorders the top-N candidates for maximum precision. Pluggable; off by default (latency cost).

The optimizer chooses the fusion/ranker as part of the plan and records which worked (fusion priors in stats).

---

## 9. Concurrency & memory model

- **Readers are lock-free.** Immutable segments + an atomically-swapped manifest mean a query takes a consistent snapshot (a set of segments) with no locking. Classic MVCC-by-immutability.
- **Segment lifetime:** readers hold a reference (Arc) to their snapshot; a merged-away segment is deleted only after all readers referencing it drain (epoch/refcount based reclamation).
- **Writers:** single-writer per index (serialized commits) keeps the commit protocol simple; ingest throughput comes from batching, not concurrent writers. (`ponytail:` global single-writer; ceiling is write throughput; upgrade path is per-shard writers under distribution.)
- **Parallelism:** per-segment search runs in parallel across a thread pool (rayon); results merged. SIMD used in distance kernels (via usearch) and bitmap ops (via roaring), with scalar fallback.
- **Memory discipline:** lexical/structured/docstore mmap'd (page-cache bounded); vector graph resident (documented budget); candidate sets are bounded by the optimizer's budgets, so a bad query can't OOM the process.

---

## 10. Explainability & observability

- `explain=true` → full plan + estimated/actual costs (§6.6).
- Structured metrics per query: plan id, per-stage in/out/latency, index touched, fusion/ranker used, estimate-vs-actual error.
- Query-history log (§5) doubles as the observability store and the learned-policy training set.
- Eval harness (`broccoli-eval`) turns judged datasets into recall@k/nDCG/latency reports for regression tracking.

---

## 11. Fault tolerance & durability

- **WAL + atomic manifest swap** → crash-consistent (see §1). Recovery = replay WAL against last committed manifest.
- **Corruption detection:** per-segment checksums in `manifest.json`; a corrupt segment is quarantined and (if replicated later) refetched.
- **Idempotent ingest:** document `id` is the key; re-adding is delete+add, so retried writes converge.
- **Backpressure:** ingest buffer bounded; over-threshold flushes or applies backpressure rather than growing unbounded (prevents data-loss-by-OOM).

---

## 12. Distribution (designed, deferred)

Not built for v1, but the design must not preclude it (per "full-fledged, not phases"):

- **Shard = a partition of segments.** Documents hash (or route by tenant/time) to shards.
- **Coordinator node** receives a query, the **optimizer plans once**, and the plan fans out to shard-local executors; partial top-k / candidate sets are merged centrally, then fused/reranked.
- **Replication:** each shard has replicas (immutable segments make replication a file copy + manifest sync); reads load-balance, writes go to a primary.
- **Consistency:** near-real-time per shard; cross-shard queries take a consistent snapshot per shard and merge (acceptable for search).
- **Why it slots in cleanly:** segments are already the data unit; execution is already staged; the optimizer already emits a serializable `Plan`. Distribution is *adding a coordinator + shard router*, not redesigning the engine.

> **`ponytail:`** single-node only in v1. The distribution section exists so we don't paint ourselves into a corner; do not build it until the single-node thesis is proven (see SHAPE.md no-gos).
