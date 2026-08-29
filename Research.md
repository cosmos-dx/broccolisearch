# Research.md — The Research Angle

This document states the scientific contribution, positions it against prior work, defines how we'll evaluate it, and lists the open problems. It's the "defend the novelty / write the paper" document.

---

## 1. The thesis (one sentence)

> **A search engine with an adaptive, cost-based query optimizer that automatically chooses indexes, candidate budgets, retrieval strategy, and ranking pipeline across heterogeneous (lexical, vector, structured, graph, temporal, semantic) indexes — and improves latency-at-fixed-recall over any single fixed strategy.**

Paper-shaped title candidates:

- *"Cost-Based Query Optimization for Heterogeneous Retrieval."*
- *"A Database-Style Optimizer for Hybrid Search: Choosing Indexes, Budgets, and Rankers per Query."*
- *"Adaptive Retrieval Planning: Learning Execution Plans Across Lexical and Vector Indexes."*

---

## 2. The gap in prior art

### What exists

- **Database CBOs (System R, Selinger et al., and every modern SQL optimizer).** Cost-based plan selection over **homogeneous, deterministic** operators (scans, index scans, joins), where cost reduces to **rows + cardinality**. Mature and beautiful — but there is no notion of an *approximate* operator with a recall/latency dial.
- **Lexical engines (Lucene/Elasticsearch/Tantivy).** Rule-based query planning over **lexical + structured** only. No native cost-based reasoning about vector retrieval.
- **Vector databases (FAISS, HNSW lib, Qdrant, Weaviate, Milvus, LanceDB).** Excellent ANN; "hybrid" search is typically a **fixed fusion** (often RRF) of BM25 + vector, applied uniformly regardless of the query.
- **Hybrid retrieval & fusion research (RRF; dense+sparse like SPLADE, ColBERT; learned sparse).** Advances *how to combine* signals, but assumes a **fixed pipeline**; it doesn't *choose the pipeline per query* under a cost budget.
- **Learned query optimizers for SQL (e.g. Neo, Bao, learned cardinality estimation).** Learn plans/cardinalities — but again for **deterministic relational** operators, not approximate retrieval operators with tunable recall.

### The precise gap

Nobody has a **cost-based optimizer that spans deterministic and approximate retrieval operators**, where:

1. "Cost" for an approximate operator is a **recall/latency curve** (parameterized by `ef_search`/`nprobe`), not a scalar.
2. The optimizer **chooses budgets** (how far to ride each curve) to meet a recall target at minimum latency.
3. Plan choice is **per-query**, conditioned on cheap query features (filter selectivity, term rarity, semantic-ness, recency).
4. Optionally, the policy is **learned from query history** (estimate-vs-actual), closing a Bao-style loop for retrieval.

That intersection — **CBO × approximate retrieval × per-query planning × learned policy** — is the contribution.

---

## 3. Formal problem statement

Given:

- A query \(q\) with intents over a subset of index axes and a recall target \(r^*\) at cutoff \(k\).
- A set of indexes \(\{I_1..I_n\}\), each exposing a cost/benefit function \(c_i(b)\) and \(\rho_i(b)\) — latency and recall as functions of a budget \(b\) (e.g. `ef_search`, candidate count).
- A space of plans \(P\): orderings of operators (filter push-down, candidate sources), fusion specs, and rankers.

Find the plan \(p^* \in P\) and per-operator budgets \(\mathbf{b}\) minimizing estimated latency subject to estimated end-to-end recall meeting the target:

\[
p^*, \mathbf{b}^* = \arg\min_{p,\mathbf{b}} \; \widehat{L}(p,\mathbf{b}) \quad \text{s.t.} \quad \widehat{R}(p,\mathbf{b}) \ge r^*
\]

where \(\widehat{L}\) composes operator latencies and \(\widehat{R}\) composes stage recalls (a candidate-stage recall ceiling propagates: you cannot rank what you didn't retrieve).

**Research sub-problems:**

- **RQ1 (estimation):** How accurately can we estimate \(\widehat{L}\) and \(\widehat{R}\) from cheap query features + offline-calibrated curves?
- **RQ2 (planning):** Does per-query plan selection beat the best *single fixed* strategy on latency-at-fixed-recall, and for which query classes?
- **RQ3 (filter interaction):** How much does cardinality-driven filter push-down (bitmap-first) improve the approximate-operator cost curve (searching survivors vs. the whole set)?
- **RQ4 (learning):** Does a policy learned from estimate-vs-actual history beat the hand-tuned rule-based policy, and how much history does it need (cold-start behavior)?
- **RQ5 (generalization):** Do learned/tuned policies transfer across datasets/workloads, or must they be recalibrated?

---

## 4. Hypotheses

- **H1:** A per-query optimizer strictly dominates the best fixed strategy on **latency-at-fixed-recall** when the workload contains a mix of query types (keyword-heavy, semantic, filtered).

  > **Status: holds on synthetic workloads, FALSIFIED as stated on real ones.** On BEIR SciFact and NFCorpus (`examples/beir_eval.py`) the optimizer is cost-competitive but *quality-dominated*: `hybrid_rrf` scores +0.046 and +0.027 nDCG@10 respectively and the optimizer never selects it. The defect is in the hypothesis's own terms — "fixed recall" was operationalized as **operator fidelity** (did the ANN find the true nearest neighbours), and an exact vector scan satisfies that at 1.0, so no plan can outrank it. Relevance recall is a different quantity, and H1 is only meaningful against the latter. See §7.3.
- **H2:** The largest single win comes from **cardinality-driven filter push-down** enabling cheaper approximate search over survivors (RQ3).
- **H3:** Offline-calibrated recall/latency curves are **accurate enough** for the optimizer to hit recall targets within a small tolerance (RQ1).
- **H4:** A learned policy improves over rules **primarily on tail/ambiguous query classes**, not on clear-cut ones (RQ4).

---

## 5. Evaluation methodology

### Datasets (with relevance judgments — required for quality claims)

- **BEIR** suite (heterogeneous zero-shot IR) — the primary generalization testbed.
- **MS MARCO** passage ranking — large-scale, well-judged.
- **TREC / Natural Questions** collections — additional judged sets.
- Optionally a **synthetic mixed workload** (deliberately blending keyword/semantic/filtered queries) to make per-class effects legible.

### Metrics

- **Quality:** recall@k, nDCG@k, MRR.
- **Efficiency:** latency p50/p95/p99.
- **North star:** **latency-at-fixed-recall** (e.g. p95 latency to reach recall@100 = 0.95).
- **Estimation quality:** predicted vs. actual latency/recall error (validates RQ1).

### Baselines (what we must beat)

1. Fixed **lexical-only** (BM25).
2. Fixed **vector-only** (HNSW at a tuned `ef_search`).
3. Fixed **hybrid (RRF)** — the strong, standard bar.
4. Fixed **filter-then-vector** at a fixed budget.
5. **Oracle** (best plan per query, chosen with hindsight) — the *upper bound* the optimizer chases.

The claim is credible iff the optimizer sits **between the best fixed baseline and the oracle**, closer to the oracle, on latency-at-fixed-recall — and reports *per-class* where it wins.

### Ablations

- Rule-based vs. learned policy (RQ4).
- With vs. without filter push-down (RQ3).
- Calibrated curves vs. naive constant cost (RQ1).
- Per-query planning vs. per-workload-static planning (isolates the "per-query" value).

---

## 6. Why this is publishable *and* a product

- **Systems venues** care about the engine + optimizer design and the latency-at-fixed-recall wins (SIGMOD/VLDB flavor).
- **IR venues** care about hybrid retrieval quality and per-class analysis (SIGIR/ECIR flavor).
- **The product** is the same artifact: an embeddable engine whose optimizer removes the "developer-as-query-planner" burden.

The dual nature is a feature: the evaluation harness that validates the paper is the same harness that guards product regressions (see Approach.md §7).

---

## 7. Open problems / honest unknowns

1. **Unified cost across deterministic + approximate operators** is genuinely unsolved; a scalar cost is wrong, so we carry (latency, recall) — but composing recall across stages (especially fusion) is an approximation whose error we must bound.
2. **Cardinality estimation for the approximate side.** Posting lists give good lexical estimates; ANN "how many good neighbors exist" is fuzzier.
3. **Fusion recall composition.** Estimating the recall of an RRF/weighted fusion of two imperfect candidate sets, cheaply, before running it.

   > **Now the blocking problem, with evidence.** Measured on BEIR (see H1): fusion is the best strategy on every real dataset tested and the optimizer never picks it. The union model `1 - Π(1 - rᵢ)` is not wrong arithmetically — it is being fed the wrong `rᵢ`. Each index reports how faithfully it computed *its own* similarity function, which says nothing about whether that function's notion of similarity matches the corpus's judgments. Lexical and vector retrieve *different* relevant documents, so fusion's gain comes from their **disagreement**, a quantity no single index can report about itself.
   >
   > What would fix it: a per-index, per-query-class estimate of *relevance* coverage, learned from judgments or from click/engagement feedback. This is derivable only from labels, not from index statistics — which is precisely why the `Policy` interface exists and why a `LearnedPolicy` is the next build rather than a nice-to-have.
4. **Learned policy cold-start & drift.** How little history is enough; how to detect workload drift and recalibrate curves.
5. **Cost of planning itself.** The optimizer must be *much* cheaper than the query it plans; plan enumeration is bounded, but this bound is a research/engineering knob. *Observed:* on the reference implementation this inverted — planning cost **27× the execution** it was planning for a hybrid query — because estimation called an accidentally O(corpus) property. It is now ~2–4× cheaper than execution on sub-millisecond queries. The threat is real and needs a standing measurement, not a one-off fix.
6. **Transfer.** Do calibrated curves / learned policies survive a dataset or embedding-model swap (RQ5)?
7. **Reproducibility of calibration.** Constants fitted from wall-clock timings are themselves random variables. With least-squares fitting, recalibrating an unchanged corpus moved constants by up to four orders of magnitude, which silently moved plan choice. A robust (Theil–Sen) fit contains this, but *how much calibration variance is tolerable before plan choice becomes unstable* is unquantified — and it bounds RQ1/H3.

These are stated up front so the design (SystemDesign.md §5–6) can be judged against them — and so the paper's threats-to-validity section writes itself.

---

## 8. Minimal experiment that proves/kills the thesis

Before any platform work, run the smallest experiment that could falsify H1 (this is also the SHAPE.md "solution sketch"):

1. One judged dataset (BEIR or MS MARCO subset).
2. Three strategies wired over Tantivy + usearch + roaring: lexical-only, vector-only, filter-then-vector.
3. A dumb rule-based picker keyed on query features (has-filter, term-rarity, semantic-ness).
4. Measure latency-at-fixed-recall: **does the picker beat every single fixed strategy?**

If **yes** → thesis stands; scale it up (more plans, learned policy, more axes).
If **no** → we learned it in weeks, not after building a distributed engine. That asymmetry is the whole point of doing the research angle first.
