#!/usr/bin/env python3
"""Evaluate the optimizer on a real judged IR dataset (BEIR format).

    python3 examples/beir_eval.py --data /tmp/scifact

Everything else in this repo is measured on synthetic corpora with synthetic
ground truth, which can only prove the optimizer is self-consistent. This runs
the same `Harness` over a real corpus, real queries, and human relevance
judgments, so the quality numbers are comparable to published baselines
(Research.md §5) and a broken analyzer or scorer shows up as a bad nDCG rather
than as a plausible-looking synthetic result.

Get a dataset (SciFact is the smallest useful one, 5k docs / 300 test queries):

    curl -sLO https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip
    unzip -q scifact.zip

BEIR layout: corpus.jsonl {_id,title,text} | queries.jsonl {_id,text}
             qrels/test.tsv  query-id \\t corpus-id \\t score
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

import numpy as np

import broccoli

MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def load(data_dir: str, split: str):
    corpus = {}
    with open(os.path.join(data_dir, "corpus.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            corpus[d["_id"]] = (d.get("title", ""), d.get("text", ""))

    qrels = defaultdict(dict)
    with open(os.path.join(data_dir, "qrels", f"{split}.tsv"), encoding="utf-8") as fh:
        next(fh)  # header
        for line in fh:
            qid, did, score = line.split()
            if float(score) > 0:
                qrels[qid][did] = float(score)

    queries = {}
    with open(os.path.join(data_dir, "queries.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if d["_id"] in qrels:      # judged queries only
                queries[d["_id"]] = d["text"]
    return corpus, queries, qrels


def embed(texts, cache_path: str):
    """Encode with a standard sentence-transformer, cached to disk.

    Cached because encoding dominates the runtime and we re-run the retrieval
    comparison far more often than the corpus changes.
    """
    if os.path.exists(cache_path):
        cached = np.load(cache_path)
        if cached["vectors"].shape[0] == len(texts):
            return cached["vectors"]
    from sentence_transformers import SentenceTransformer
    print(f"  encoding {len(texts)} texts with {MODEL} (first run only)...")
    model = SentenceTransformer(MODEL)
    vectors = model.encode(texts, batch_size=64, convert_to_numpy=True,
                           show_progress_bar=False, normalize_embeddings=True)
    np.savez_compressed(cache_path, vectors=vectors)
    return vectors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="BEIR dataset directory")
    ap.add_argument("--split", default="test")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--recall-target", type=float, default=0.70,
                    help="IR recall@k bar a strategy must clear to be compared")
    args = ap.parse_args()

    name = os.path.basename(os.path.normpath(args.data))
    corpus, queries, qrels = load(args.data, args.split)
    print(f"\n{name}: {len(corpus)} docs, {len(queries)} judged queries, "
          f"{sum(len(v) for v in qrels.values())} judgments")

    doc_ids = list(corpus)
    doc_text = [f"{corpus[d][0]} {corpus[d][1]}".strip() for d in doc_ids]
    query_ids = list(queries)

    started = time.perf_counter()
    doc_vecs = embed(doc_text, os.path.join(args.data, f"_docvecs_{name}.npz"))
    query_vecs = embed([queries[q] for q in query_ids],
                       os.path.join(args.data, f"_qvecs_{name}_{args.split}.npz"))
    print(f"  embeddings ready in {time.perf_counter() - started:.1f}s "
          f"(dim={doc_vecs.shape[1]})")

    idx = broccoli.Index.create(schema={
        "title": broccoli.Text(analyzer="english"),
        "text": broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=int(doc_vecs.shape[1]), metric="cosine"),
    })
    for i, did in enumerate(doc_ids):
        title, body = corpus[did]
        idx.add({"id": did, "title": title, "text": body,
                 "embedding": doc_vecs[i].tolist()})
    started = time.perf_counter()
    idx.calibrate()
    print(f"  indexed + calibrated in {time.perf_counter() - started:.1f}s")

    judgments = [
        broccoli.Judgment(
            query={"text": queries[q], "semantic": query_vecs[i].tolist()},
            relevant=qrels[q])
        for i, q in enumerate(query_ids)
    ]

    # Held-out split. A bucketed mean will memorise the queries it was fitted
    # to, so the learned policy is trained and scored on disjoint sets.
    rng = np.random.default_rng(0)
    order = rng.permutation(len(judgments))
    cut = len(judgments) // 2
    train = [judgments[i] for i in order[:cut]]
    test = [judgments[i] for i in order[cut:]]

    harness = broccoli.Harness(idx, test, k=args.k,
                               recall_target=args.recall_target)
    reports = harness.compare()

    learned = broccoli.LearnedPolicy().fit(idx, train, k=args.k)
    print(f"\ntrained on {len(train)} held-out queries, scored on {len(test)}")
    print(learned.describe())
    previous, idx.optimizer.policy = idx.optimizer.policy, learned
    reports.insert(1, harness.run_strategy("ADAPTIVE (learned)", None))
    idx.optimizer.policy = previous

    print(harness.report(reports=reports))

    print(f"nDCG@{args.k} is the metric BEIR leaderboards report, so these are "
          f"directly comparable\nto published baselines for {name}.\n")

    # The thesis on real data has TWO halves, and reporting only the cost half
    # would hide a failure: the optimizer must not just be cheap, it must not
    # give up relevance to get there.
    # The point of a relevance-aware cost model: `recall` must actually buy
    # quality. If the curve below is flat, the optimizer is ignoring the knob.
    coverage = "  ".join(f"{n}={v:.3f}"
                         for n, v in sorted(idx.optimizer.solo_coverage.items()))
    print(f"\nrecall target sweep (measured solo coverage: {coverage}):")
    print(f"{'recall=':>10}{'nDCG@' + str(args.k):>10}{'work':>10}   plan mix")
    for target in (0.30, 0.50, 0.70, 0.90):
        for judgment in test:
            judgment.query["recall"] = target
        idx.stats.history.clear()
        report = harness.run_strategy(f"adaptive@{target}", None)
        mix = "  ".join(f"{n}={c}" for n, c in
                        sorted(idx.stats.plan_counts().items()))
        print(f"{target:>10.2f}{report.ndcg:>10.4f}{report.work:>10.0f}   {mix}")
    for judgment in test:
        judgment.query.pop("recall", None)

    fixed = [r for r in reports if not r.name.startswith("ADAPTIVE")]
    best_quality = max(fixed, key=lambda r: r.ndcg)
    for report in reports:
        if not report.name.startswith("ADAPTIVE"):
            continue
        gap = best_quality.ndcg - report.ndcg
        if gap > 0.005:
            verdict = f"gives up {gap:.4f} nDCG to {best_quality.name}"
        elif report.work < best_quality.work * 0.95:
            verdict = (f"matches {best_quality.name} for "
                       f"{best_quality.work / max(report.work, 1):.1f}x less work")
        else:
            verdict = (f"reaches {best_quality.name}'s quality, at its cost too "
                       f"— no free lunch on this workload")
        print(f"{report.name:<22} nDCG@{args.k}={report.ndcg:.4f} "
              f"work={report.work:>6.0f}  — {verdict}")

    print("\nThe optimizer's value here is landing on the right point of the "
          "quality/cost curve\nfor the requested recall without being told "
          "which index to use — not a free saving.\nA per-query routing WIN "
          "needs a workload of mixed query shapes (Research.md H1);\nboth of "
          "these datasets are homogeneous, so there is little to route.")


if __name__ == "__main__":
    main()
