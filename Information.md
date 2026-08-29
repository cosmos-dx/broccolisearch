# Information.md — Domain Knowledge Base

This is the "learn/refresh the fundamentals" document. Every design decision in BroccoliSearch assumes the concepts below. If a term in another doc is unclear, it is defined here.

---

## 1. The ELK / Elastic Stack (the thing we are inspired by and going beyond)

**ELK** = **E**lasticsearch + **L**ogstash + **K**ibana (now "Elastic Stack" after Beats was added).

```
data sources → Beats/Logstash → Elasticsearch → Kibana
              (collect/parse)    (index/search)   (visualize)
```

- **Elasticsearch** — the search/storage/analytics engine. A distributed wrapper around **Apache Lucene** that adds clustering, sharding, replication, and a REST/JSON API.
- **Logstash** — ingestion pipeline: collect, parse, transform data before indexing.
- **Kibana** — dashboards/visualization on top.
- **Beats** — lightweight shippers (Filebeat, Metricbeat) that push data in.

**What ELK does well:** lexical search, structured filters, aggregations, logs/observability, horizontal scale.

**What ELK does *not* do:** it has a **rule-based** query planner that only understands lexical + structured indexes. It has no native, cost-based reasoning about vector/semantic retrieval. That gap is the BroccoliSearch opportunity.

---

## 2. Why traditional search is fast: the inverted index

A row-store `SELECT ... WHERE text LIKE '%broccoli%'` scans every row — O(N). Search engines flip the problem: they store **word → documents** instead of **document → words**.

Documents:

```
doc1: "broccoli is green"
doc2: "green search engine"
```

Inverted index:

```
broccoli → [doc1]
is       → [doc1]
green    → [doc1, doc2]
search   → [doc2]
engine   → [doc2]
```

Searching `green` becomes a **dictionary lookup** returning `[doc1, doc2]` — no scanning. Multi-term queries **intersect/union posting lists** (`green AND search` → intersect the two lists). Cost is O(matching terms), not O(total documents).

### The tricks that make it *actually* fast

1. **Analysis at write time.** Tokenize, lowercase, stem (`running → run`), drop stopwords — all done **once at ingest**, so queries stay cheap. Trade: slower writes, faster reads.
2. **Term dictionary as an FST (Finite State Transducer).** Compressed, prefix-shared structure that fits in memory; enables instant term lookup and fast prefix/wildcard/autocomplete.
3. **Immutable segments.** Data is written into immutable **segments**; they are cached in the OS page cache (reads become RAM reads), need no read locks, and enable aggressive caching. New data → new segments, merged in the background.
4. **Skip lists + block compression.** Posting lists skip ahead during intersection and are block-compressed so more fits in cache.
5. **Doc values (columnar).** A column-oriented copy on disk for fast sorting/aggregation, separate from the inverted index used for matching.

### BM25 — the lexical scoring function

BM25 scores how relevant a document is to a query term, balancing:

- **Term frequency (TF):** more occurrences → higher score, with **diminishing returns** (saturation).
- **Inverse document frequency (IDF):** rare terms are more informative → weighted higher.
- **Document length normalization:** long documents don't get an unfair advantage.

It's the strong lexical baseline every hybrid system still leans on.

---

## 3. Embeddings and semantic search

### What an embedding is

An **embedding** is a dense vector (e.g. 384/768/1536 floats) produced by a model such that **semantically similar inputs land near each other** in vector space. "car" and "automobile" have distant *spellings* but nearby *embeddings*.

- Lexical search matches **tokens**. It fails on synonyms, paraphrase, and cross-lingual queries.
- Vector search matches **meaning**. It fails on exact IDs, rare tokens, codes, and precise keyword intent ("error code E-4021").

This is exactly why **hybrid** matters: the two failure modes are complementary.

### Distance / similarity metrics

- **Cosine similarity** — angle between vectors; the most common for text embeddings.
- **Dot product** — used when embeddings are normalized or trained for it.
- **Euclidean (L2)** — straight-line distance.

### The cost problem

A brute-force nearest-neighbour search compares the query against **every** vector — O(N·d). At millions of vectors that's too slow for online search. Hence **ANN**.

---

## 4. Approximate Nearest Neighbour (ANN)

ANN trades a little recall for a lot of speed. Two dominant families:

### HNSW (Hierarchical Navigable Small World)

- A multi-layer graph. Upper layers are sparse (long hops), lower layers dense (fine-grained).
- Search starts at the top, greedily hops toward the query, descends layer by layer.
- **Pros:** excellent recall/latency, great for high-dimensional vectors, incremental inserts.
- **Cons:** memory-hungry (the graph lives in RAM), slower builds.
- **Key knobs:** `M` (graph connectivity), `ef_construction` (build quality), `ef_search` (**the recall/latency dial at query time** — higher = more recall, more latency).

### IVF (Inverted File Index)

- Cluster vectors into cells (via k-means); at query time only search the nearest few cells (`nprobe`).
- Often combined with **PQ (Product Quantization)** to compress vectors (IVF-PQ) for huge datasets.
- **Pros:** memory-efficient, scales to billions.
- **Cons:** recall depends heavily on `nprobe`; needs training.

> **Why this matters for BroccoliSearch:** `ef_search` / `nprobe` are exactly the **candidate-budget knobs** the cost-based optimizer will set per query. ANN cost is *not* a fixed number — it's a **recall vs. latency curve** the planner rides.

---

## 5. Bitmap indexes (the underrated primitive)

A **bitmap index** stores, for each value of an attribute, a bitset marking which documents have it:

```
status=active   → 1 0 1 1 0 ...
status=deleted  → 0 1 0 0 1 ...
country=IN      → 1 1 0 1 0 ...
```

- Filtering `status=active AND country=IN` is a **bitwise AND** of two bitsets — nearly free, SIMD-friendly.
- **Roaring bitmaps** compress these efficiently (hybrid of arrays/bitmaps/run-length) and are the industry standard (used by Lucene, Druid, ClickHouse, etc.).

**The key insight:** structured filters via bitmaps let you shrink the candidate universe *before* paying for expensive vector search. "Don't vector-search your whole database" is implemented with bitmaps.

---

## 6. The two-stage retrieval pattern

Nearly every serious search system uses this shape:

```
        millions of docs
              │
   ┌──────────▼───────────┐
   │  Candidate generation │   cheap, high recall
   │  (lexical / ANN /     │   → hundreds/thousands of candidates
   │   bitmap filters)     │
   └──────────┬───────────┘
              │
   ┌──────────▼───────────┐
   │      Ranking          │   expensive, high precision
   │  (BM25 + vector +     │   → final ordered top-k
   │   cross-encoder /      │
   │   learned fusion)     │
   └──────────┬───────────┘
              │
           top-k results
```

- **Stage 1 (recall):** get *all the right answers in the pool*, cheaply. Missing a doc here means it can never be returned.
- **Stage 2 (precision):** order the pool well, possibly with an expensive model (e.g. a **cross-encoder** reranker that reads query+doc together).

BroccoliSearch's optimizer decides *which* recall sources to use, *how big* the candidate pool is, and *which* ranker runs.

---

## 7. Hybrid ranking / fusion

Combining evidence from multiple indexes:

- **RRF (Reciprocal Rank Fusion):** score a doc by `Σ 1/(k + rank_i)` across each list it appears in. **~10 lines of code, shockingly strong, needs no tuning.** Our baseline.
- **Weighted score fusion:** linear combo of normalized scores (`α·BM25 + β·cosine`). Needs score normalization + tuning.
- **Learned fusion / Learning-to-Rank (LTR):** a model (e.g. LambdaMART, or a neural reranker) learns the combination from labeled data. Highest ceiling, needs training data.

---

## 8. Information Retrieval metrics (how we *score* ourselves)

We never ship a retrieval change we can't measure. Standard metrics, all needing labeled relevance judgments:

- **Recall@k:** fraction of all relevant docs that appear in the top-k. Measures the *recall stage*.
- **Precision@k:** fraction of top-k that are relevant.
- **MRR (Mean Reciprocal Rank):** `1/rank` of the first relevant result, averaged. Good for "one right answer" queries.
- **nDCG@k (Normalized Discounted Cumulative Gain):** graded relevance with position discounting — the gold-standard ranking metric.
- **Latency percentiles:** p50/p95/p99. Always reported *with* quality (see below).
- **Latency-at-fixed-recall:** our north-star operational metric — "how fast can you hit recall@100 = 0.95?" This is what an adaptive optimizer is supposed to improve.

### Labeled datasets we can use

- **BEIR** — a benchmark suite of heterogeneous IR datasets with judgments; great for zero-shot hybrid evaluation.
- **MS MARCO** — large passage-ranking dataset with relevance labels.
- **Natural Questions / TREC** collections — additional judged sets.

Using these means we can *prove* the optimizer helps, instead of asserting it.

---

## 9. The six index axes (BroccoliSearch's unifying model)

BroccoliSearch treats "an index" abstractly: anything that can produce candidate doc IDs and/or scores. Six axes:

| Axis | Answers questions like | Backed by |
|---|---|---|
| **Lexical** | "documents containing these words" | inverted index (Tantivy) |
| **Vector** | "documents meaning something similar" | ANN / HNSW (usearch/FAISS) |
| **Structured** | "documents where status=active, price<100" | bitmap / columnar (roaring) |
| **Graph** | "documents connected to X within 2 hops" | adjacency / graph index *(deferred)* |
| **Temporal** | "documents in this time window / recency-decayed" | time-partitioned index *(deferred)* |
| **Semantic** | "documents matching this concept/entity/intent" | embeddings + KG signals *(built on vector+structured)* |

The **research claim**: a single **cost-based optimizer** can reason across all six and pick the cheapest plan meeting a recall target — the way a SQL optimizer reasons across table scans, index scans, and joins.

---

## 10. Why a cost-based optimizer is hard here (the crux)

SQL cost-based optimizers work because everything reduces to **rows + cardinality statistics** and cost is a scalar (estimated rows/IO). In search:

- **Lexical** cost is fairly deterministic (posting-list lengths → good cardinality estimates).
- **Vector** cost is **approximate**: "cost" is a **recall vs. latency curve** parameterized by `ef_search`/`nprobe`. There is no single scalar.
- **Fusion quality** depends on the *query type* (keyword-heavy vs. semantic vs. filtered), which you must *estimate from the query itself*.

So the central research problem is: **define a unified cost/benefit model that spans deterministic and approximate indexes, and a planner that uses it to choose plans + budgets per query.** That is what makes this novel rather than an integration project. (Expanded in **Research.md**.)
