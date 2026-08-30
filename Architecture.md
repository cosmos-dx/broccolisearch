# Architecture.md — Component Architecture

This document describes *how the pieces fit together*: the layers, crates/modules, their boundaries, and the data flow. For the deep internals of each subsystem see **SystemDesign.md**.

---

## 1. Architectural principles

1. **Rent the indexes, own the planner.** Index engines are dependencies; the optimizer is the product.
2. **Everything is an `Index` behind a trait.** Lexical, vector, structured, (later) graph/temporal all implement one interface. The optimizer only sees the trait, never the concrete engine.
3. **Intent in, strategy out.** The API accepts *what* the caller wants; the engine decides *how*.
4. **Coarse FFI boundary.** Python↔Rust crossings are per-operation (index/search), never per-document-in-a-loop. Zero-copy where possible.
5. **Measurable by construction.** Every operator emits stats (candidates in/out, latency, estimated vs. actual cost).

---

## 2. The layer cake

```
┌───────────────────────────────────────────────────────────┐
│  Clients:  Python API  │  broccolid server  │  CLI          │
└───────────────┬───────────────────┬───────────────────────┘
                │  PyO3 / gRPC/JSON  │
┌───────────────▼───────────────────▼───────────────────────┐
│                     broccoli-core (Rust)                    │
│                                                             │
│   ┌──────────────┐   ┌───────────────────────────────┐     │
│   │  Query API    │──▶│      broccoli-optimizer        │     │
│   │ (intent obj)  │   │  cost model · planner · policy │     │
│   └──────────────┘   └───────────────┬───────────────┘     │
│                                       │ execution plan       │
│                       ┌───────────────▼───────────────┐     │
│                       │        Execution Engine         │     │
│                       │  operators · candidate budgets  │     │
│                       └───────┬───────────┬────────────┘     │
│                               │           │                  │
│         ┌─────────────────────┼───────────┼──────────────┐   │
│         ▼                     ▼           ▼              ▼   │
│   ┌───────────┐        ┌───────────┐ ┌───────────┐ ┌────────┐│
│   │  Lexical   │        │  Vector    │ │Structured │ │Ranking ││
│   │  Index     │        │  Index     │ │  Index    │ │ /Fusion││
│   │ (Tantivy)  │        │ (usearch)  │ │ (roaring) │ │        ││
│   └─────┬─────┘        └─────┬─────┘ └─────┬─────┘ └────────┘│
│         └────────────────────┴─────────────┘                 │
│                          │                                    │
│                 ┌────────▼─────────┐                          │
│                 │  Storage Engine   │  segments · mmap · WAL   │
│                 └────────┬─────────┘                          │
│                          │                                    │
│                 ┌────────▼─────────┐                          │
│                 │  Statistics Store │  card. stats · histories │
│                 └──────────────────┘                          │
└───────────────────────────────────────────────────────────┘
```

---

## 3. Crate / module layout

A Cargo workspace. Each crate has one responsibility; the optimizer depends on abstractions, not concrete engines.

```
broccolisearch/
├── crates/
│   ├── broccoli-core/          # facade: Index, Document, Query, Schema, orchestration
│   ├── broccoli-index/         # the Index trait + shared candidate/scoring types
│   │   ├── lexical/            #   Tantivy adapter
│   │   ├── vector/             #   usearch/HNSW adapter
│   │   └── structured/         #   roaring bitmap + columnar adapter
│   ├── broccoli-optimizer/     # cost model, plan enumeration, planner, policy trait
│   ├── broccoli-exec/          # execution engine: operators, budgets, stage stats
│   ├── broccoli-rank/          # BM25 wiring, RRF, weighted fusion, reranker hook
│   ├── broccoli-storage/       # segments, mmap, WAL, commit/merge, schema store
│   ├── broccoli-stats/         # statistics store + query-history log
│   ├── broccoli-eval/          # evaluation harness (recall/nDCG/latency) + dataset loaders
│   ├── broccoli-py/            # PyO3 bindings → python package `broccoli`
│   └── broccolid/              # server binary (JSON/gRPC) + CLI
├── python/                     # python package scaffolding, type stubs, examples
├── datasets/                   # (gitignored) BEIR/MS MARCO fixtures for eval
└── docs/                       # these documents
```

### Dependency direction (must stay acyclic)

```
broccolid / broccoli-py
        │
        ▼
   broccoli-core
        │
        ├──▶ broccoli-optimizer ──▶ broccoli-stats
        │            │
        │            └──▶ broccoli-index (trait only)
        ├──▶ broccoli-exec ──▶ broccoli-index ──▶ {lexical, vector, structured}
        ├──▶ broccoli-rank
        └──▶ broccoli-storage ──▶ broccoli-stats
```

**Rule:** `broccoli-optimizer` depends only on the `broccoli-index` **trait** and `broccoli-stats`. It must be possible to unit-test the optimizer with mock indexes and synthetic statistics, no real engine attached.

---

## 4. Key abstractions

### 4.1 The `Index` trait (the unifying interface)

Every index axis implements this. The optimizer reasons about all axes uniformly through it.

```rust
/// A source of candidate documents and/or scores for a query.
pub trait Index {
    /// Which query capabilities this index can serve.
    fn capabilities(&self) -> Capabilities;

    /// Cheap estimate the optimizer uses to plan — WITHOUT executing.
    /// Returns a cost/benefit estimate as a function of the requested budget.
    fn estimate(&self, q: &SubQuery, budget: Budget) -> CostEstimate;

    /// Produce candidates (and optional scores) under a budget.
    fn search(&self, q: &SubQuery, budget: Budget) -> CandidateSet;

    /// Statistics the cost model needs (cardinalities, sizes, curves).
    fn statistics(&self) -> IndexStats;
}
```

- `Capabilities` — e.g. lexical supports term match; vector supports ANN; structured supports filter/range.
- `Budget` — the tunable knob (candidate count, `ef_search`, `nprobe`). **Cost is a function of budget**, not a scalar (see Information.md §4/§10).
- `CostEstimate` — `{ est_latency, est_recall, est_cardinality }` — a point on the recall/latency curve for that budget.
- `CandidateSet` — doc IDs + optional per-index scores + provenance (which index produced them).

### 4.2 The `Query` (intent) object

```rust
pub struct Query {
    pub text: Option<TextIntent>,        // keyword intent
    pub semantic: Option<VectorIntent>,  // embedding / semantic intent
    pub filter: Option<Filter>,          // structured predicates (bitmap-served)
    pub time: Option<TimeConstraint>,    // temporal window / recency decay
    pub k: usize,                        // top-k
    pub target: Target,                  // recall/latency target OR "auto"
    pub explain: bool,
    pub pin: Option<PlanOverride>,       // manual strategy override (debug/bench)
}
```

### 4.3 The `Plan` (what the optimizer emits)

```rust
pub struct Plan {
    pub steps: Vec<PlanStep>,   // ordered operators with chosen budgets
    pub fusion: FusionSpec,     // how candidate sets combine
    pub ranker: RankerSpec,     // final ranking stage
    pub estimate: PlanEstimate, // predicted latency/recall (compared to actual after run)
}
```

### 4.4 The `Policy` trait (rule-based now, learned later)

```rust
/// Chooses among enumerated candidate plans. Swappable without API change.
pub trait Policy {
    fn choose(&self, candidates: &[Plan], ctx: &QueryContext) -> Plan;
    fn observe(&mut self, plan: &Plan, actual: &Execution);  // feed history for learning
}
```

- `RuleBasedPolicy` — hand-tuned cost model (v1).
- `LearnedPolicy` — trained on query history (later); same interface, so nothing above it changes.

---

## 5. Data flow

### 5.1 Indexing path

```
Document (Python/JSON)
   → broccoli-core: validate against Schema
   → route fields:  text → Lexical | vector → Vector | typed → Structured
   → broccoli-storage: buffer → immutable segment → atomic commit (+WAL)
   → broccoli-stats: update cardinality/statistics
   → background: segment merge
```

### 5.2 Query path (the important one)

```
Query (intent)
   │
   ▼
broccoli-optimizer
   1. featurize query        (has filter? term rarity? semantic-heavy? time?)
   2. ask each Index.estimate() at candidate budgets    ← uses broccoli-stats
   3. enumerate candidate plans (order, budgets, fusion, ranker)
   4. Policy.choose() the min-estimated-cost plan meeting recall Target
   │
   ▼  execution plan
broccoli-exec
   5. run operators in order, honoring budgets
      (e.g. structured filter → bitmap; then ANN over survivors; then BM25)
   6. fuse candidate sets (broccoli-rank: RRF / weighted)
   7. rank/rerank → top-k
   │
   ▼
Results (+ Plan if explain) 
   8. Policy.observe(plan, actual)  → broccoli-stats query-history log
```

Step 8 closes the loop: actual vs. estimated cost is logged, which is the training signal for the learned policy later — **without** changing any interface.

---

## 6. Language & interop choices

| Concern | Choice | Why |
|---|---|---|
| Core engine | **Rust** | control over memory, mmap, SIMD, concurrency; the whole index ecosystem (Tantivy, usearch, roaring, PyO3) is Rust-native. |
| Bindings | **PyO3** | first-class Python for embeddings + experimentation; coarse boundary. |
| Server | Rust (`broccolid`) | axum/tonic for JSON/gRPC. |
| Lexical index | **Tantivy** | mature Rust Lucene; do not rebuild. |
| Vector index | **usearch** (or FAISS via bindings) | strong HNSW, arm64/x86 SIMD, mmap-able. |
| Structured/bitmap | **roaring** | industry-standard compressed bitmaps. |
| Storage | custom thin layer over mmap + segment files | we control layout; engines plug into it. |

See **Approach.md** for the reuse ladder that produced these choices.

### 6.1 Status: the first engine is ported

`broccoli-core/` is a real PyO3 extension holding a native inverted index and
BM25 scan. It is optional — absent it, the Python path runs and the whole suite
still passes — and it is selected per `LexicalIndex` instance at construction,
so both backends can be exercised in one process.

**What this validated is the interface claim, not the language choice.** The
optimizer, the cost model, the calibration routines and all 71 tests were
unchanged by the swap. That is the property §4.1 asserts and it had never been
tested before, because there had only ever been one implementation of
`BaseIndex` to depend on.

Three findings worth carrying into the remaining ports:

1. **Not everything should be ported.** The vector engine stays numpy. Its exact
   scan is one matmul already executing in BLAS with SIMD, so a hand-written
   Rust loop would have been slower. "Port the hot loop" is the rule; "port
   everything" is how you spend a week to lose a benchmark.
2. **Keep analysis on the Python side.** Tokenising and stemming happen once per
   document, not once per posting, so they are not hot — and two stemmer
   implementations would eventually disagree, which silently destroys recall
   when index-time and query-time analysis drift apart. Rust receives tokens,
   never text.
3. **The FFI boundary is a cost the cost model must know about.** See §6.2 below;
   this one caused a real bug.

Measured: a large speedup on long posting-list scans, tapering to none on short
ones, and the cost model's error on keyword queries fell from 43.5% to 8.9% —
the sub-0.05ms error was substantially an artefact of timing an interpreter,
rather than a modelling failure.

### 6.2 The boundary has a price, and the estimator has to be told

The rule in §1.4 is that FFI crossings are per-operation, never
per-document-in-a-loop. That is necessary but was not sufficient. Handing a
filter's surviving-document set to Rust costs **O(|domain|) whatever Rust then
does with it**, while the lexical cost model charges `min(df, |domain|)`. A
4,000-document filter pushed onto a 50-document posting list therefore paid
4,000 units of marshalling the estimator could not see, and filtered keyword
queries went from 23.7% error to 68.9%.

The fix was to choose the join order **above** the boundary rather than inside
Rust: when the posting lists are the smaller side they are scanned unfiltered
and non-members are dropped in Python, so the domain never crosses at all. Both
branches then cost the `min(df, |domain|)` the model already estimates.

Generalised, for every engine still to be ported: **an operator is only worth
making faster if the cost model still describes it afterwards.** An optimizer
that mispredicts its own fastest operator will route queries away from it, and
the port will show up as a regression.

### 6.3 Why not Go?

Go is a reasonable question for this system and worth answering explicitly rather than by omission, because it beats Rust on the two things this project has actually spent its time on: build/iterate speed, and how quickly a contributor can get productive. The optimizer — the part that carries the whole thesis — is ordinary business logic over statistics, and Go would express it just as well.

The decision goes the other way for one reason that is specific to this project rather than a general preference:

| | Rust | Go |
|---|---|---|
| Lexical / vector / bitmap engines | Tantivy, usearch, roaring — mature, and the ones this design explicitly rents rather than writes | Bluge (a Bleve fork, less active), no first-class HNSW; more of the engine becomes ours to build and maintain |
| Cost of a mistake in the hot loop | bounds-checked, no GC pauses in a p99 latency budget | GC pauses land exactly in the tail latency this project's headline metric measures |
| Python bindings | PyO3, ergonomic and coarse-grained | cgo, which is a real tax at the boundary |

The **[Approach.md](./Approach.md) reuse ladder is what decides this**: the directive is "rent the indexes, own the planner". Rust is where the rentable indexes live. Choosing Go would mean writing more of the layer this project deliberately refuses to write, to save effort on the layer it actually cares about — backwards.

Two things worth being honest about. First, the argument above is about the *engines*, not the planner: everything the optimizer does is still Python, and nothing in the measured results depends on the core language. Second, **the interfaces matter far more than the language** — `BaseIndex` and `Policy` are what the optimizer depends on. A Go core behind the same two interfaces would be a worse fit for the reasons above, not an incorrect one, and the optimizer would not know the difference.

§6.1 is now evidence for that second claim rather than a prediction of it. Swapping the lexical engine for a native implementation changed no interface, no caller and no test — which is exactly what should happen, and is the reason the language question stays a trade-off about ecosystems instead of a rewrite.

---

## 7. What is deliberately *not* here (yet), and where it will slot in

| Deferred axis/feature | Where it plugs in later | Why the architecture already supports it |
|---|---|---|
| **Graph index** | new `broccoli-index/graph` impl of `Index` trait | optimizer only sees the trait; add capability + cost model. |
| **Temporal index** | `broccoli-index/temporal` + `TimeConstraint` already in `Query` | query object + estimate interface already carry time. |
| **Learned planner** | `LearnedPolicy: Policy`, trained from `broccoli-stats` history | `Policy` trait + observe() loop already log history. |
| **Distribution** | shard = a set of segments; a coordinator fans out `search` and merges | segments are already the unit of data; execution is already stage-based. |

The point of designing these in now (per your "full-fledged, not phases" instruction) is that **adding them is an implementation, never a rewrite**.
