"""The public `Index` — the whole library behind one object (PRD.md §8).

    idx = broccoli.Index.create("./products.broccoli", schema={...})
    idx.add({...})
    hits = idx.search(text="...", where={...}, k=10, explain=True)

The caller states intent and constraints. The optimizer owns strategy.
"""

from __future__ import annotations

import json
import os
import time
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple, Union)

from . import ranking
from .calibration import TIMING_REPEATS, fit_linear
from .execution import Executor
from .indexes import LexicalIndex, StructuredIndex, VectorIndex
from .optimizer import OVERFETCH, Optimizer, Policy, RuleBasedPolicy
from .query import (Eq, Explain, Hit, OneOf, Predicate, Query, Range, Results,
                    normalize_where, parse_duration)
from .schema import STRUCTURED_KINDS, Schema, to_timestamp
from .stats import StatisticsStore
from .types import Budget

Embedder = Callable[[str], Sequence[float]]


class Index:
    """A searchable corpus: schema + three index engines + the optimizer."""

    def __init__(self, schema: Schema, path: Optional[str] = None,
                 embedder: Optional[Embedder] = None,
                 policy: Optional[Policy] = None):
        self.schema = schema
        self.path = path
        self.embedder = embedder

        self._deleted: set = set()          # shared with every index engine
        self._ext_ids: List[str] = []       # internal int id -> external str id
        self._id_of: Dict[str, int] = {}
        self._docs: Dict[int, Dict[str, Any]] = {}

        self.lexical = LexicalIndex(
            {n: schema.fields[n].analyzer for n in schema.text_fields},
            deleted=self._deleted) if schema.text_fields else None

        vf = schema.vector_field()
        if vf:
            spec = schema.fields[vf]
            self.vector = VectorIndex(vf, spec.dim, spec.metric, spec.m,
                                      spec.ef_construction, deleted=self._deleted)
        else:
            self.vector = None

        structured_fields = {n: schema.fields[n].kind
                             for n in schema.structured_fields}
        self.structured = (StructuredIndex(structured_fields, deleted=self._deleted)
                           if structured_fields else None)

        self.stats = StatisticsStore()
        self.optimizer = Optimizer(self.lexical, self.vector, self.structured,
                                   self.stats, policy or RuleBasedPolicy())
        self.executor = Executor(self.lexical, self.vector, self.structured)
        self._calibrated = False
        self._time_field = next(iter(schema.names_of("datetime")), None)

    # ------------------------------ lifecycle ------------------------------ #
    @classmethod
    def create(cls, path: Optional[str] = None,
               schema: Union[Schema, Dict[str, Any], None] = None,
               embedder: Optional[Embedder] = None,
               policy: Optional[Policy] = None,
               overwrite: bool = False) -> "Index":
        if schema is None:
            raise ValueError("create() needs a schema")
        if isinstance(schema, dict):
            schema = Schema(schema)
        if path and os.path.exists(path) and not overwrite:
            raise FileExistsError(
                f"'{path}' already exists; use Index.open() or overwrite=True")
        if path:
            os.makedirs(path, exist_ok=True)
        return cls(schema, path=path, embedder=embedder, policy=policy)

    @classmethod
    def open(cls, path: str, embedder: Optional[Embedder] = None,
             policy: Optional[Policy] = None) -> "Index":
        meta_path = os.path.join(path, "meta.json")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(f"no broccoli index at '{path}'")
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        idx = cls(Schema.from_spec(meta["schema"]), path=path,
                  embedder=embedder, policy=policy)
        docs_path = os.path.join(path, "docs.jsonl")
        if os.path.exists(docs_path):
            with open(docs_path, encoding="utf-8") as fh:
                idx.add_many(json.loads(line) for line in fh if line.strip())
        idx.stats = StatisticsStore.load(os.path.join(path, "stats.json"))
        idx.optimizer.stats = idx.stats
        return idx

    def save(self, path: Optional[str] = None) -> str:
        """Persist the corpus.

        ponytail: rewrites one jsonl (vectors inline) instead of immutable
        segments + WAL. Ceiling: O(corpus) write per save, no crash-safe
        incremental commit. Upgrade path: the segment/manifest protocol in
        SystemDesign.md §1, which is a Rust-port concern.
        """
        path = path or self.path
        if not path:
            raise ValueError("no path given and this index is in-memory")
        os.makedirs(path, exist_ok=True)
        tmp = os.path.join(path, "docs.jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for internal_id, doc in self._docs.items():
                if internal_id in self._deleted:
                    continue
                fh.write(json.dumps(doc) + "\n")
        os.replace(tmp, os.path.join(path, "docs.jsonl"))  # atomic swap
        with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": self.schema.spec(),
                       "n_docs": len(self)}, fh, indent=2)
        self.stats.n_docs = len(self)
        self.stats.index_stats = self._index_stats()
        self.stats.save(os.path.join(path, "stats.json"))
        self.path = path
        return path

    def commit(self) -> "Index":
        """Calibrate the cost model against reality, then persist if on disk."""
        self.calibrate()
        if self.path:
            self.save()
        return self

    # ------------------------------ writing -------------------------------- #
    def add(self, doc: Dict[str, Any]) -> str:
        clean = self.schema.validate(doc)
        ext_id = clean["id"]
        if ext_id in self._id_of:
            self.delete(ext_id)
        internal = len(self._ext_ids)
        self._ext_ids.append(ext_id)
        self._id_of[ext_id] = internal
        self._docs[internal] = clean

        if self.lexical:
            self.lexical.add(internal, clean)
        if self.structured:
            self.structured.add(internal, {
                n: clean[n] for n in self.schema.structured_fields if n in clean})
        vf = self.schema.vector_field()
        if self.vector and vf in clean:
            self.vector.add(internal, clean[vf])
        self._calibrated = False
        return ext_id

    def add_many(self, docs: Iterable[Dict[str, Any]]) -> int:
        return sum(1 for doc in docs if self.add(doc))

    def delete(self, ext_id: str) -> bool:
        internal = self._id_of.pop(str(ext_id), None)
        if internal is None:
            return False
        # Tombstone: every engine shares this set and skips these ids.
        self._deleted.add(internal)
        if self.lexical:
            self.lexical.remove(internal, self._docs.get(internal))
        if self.structured:
            self.structured.remove(internal)
        if self.vector:
            self.vector.remove(internal)
        return True

    def __len__(self) -> int:
        return len(self._ext_ids) - len(self._deleted)

    def get(self, ext_id: str) -> Optional[Dict[str, Any]]:
        internal = self._id_of.get(str(ext_id))
        return self._docs.get(internal) if internal is not None else None

    # ---------------------------- calibration ------------------------------ #
    def calibrate(self) -> None:
        """Measure this machine and this corpus. A cost model built on
        hardcoded constants is confidently wrong (Approach.md §3)."""
        if self.lexical and self.lexical.n_docs:
            # Sample across the WHOLE document-frequency range, not just the
            # head: calibrating on common terms only would fit a slope with no
            # information about fixed per-query cost.
            by_df = sorted(self.lexical.postings, key=self.lexical.df)
            if by_df:
                step = max(1, len(by_df) // 30)
                self.lexical.calibrate(by_df[::step][:30] + by_df[-5:])
        if self.vector and len(self) > 1:
            self.vector.calibrate()
        if self.structured and self.structured.n_docs:
            self.structured.calibrate(self._filter_calibration_samples())
        (self.optimizer.fusion_ms_per_doc,
         self.optimizer.rank_ms_per_doc) = ranking.calibrate()
        self._calibrated = True
        (self.optimizer.pipeline_ms,
         self.optimizer.pipeline_ms_per_hit) = self._measure_pipeline_overhead()
        self.optimizer.solo_coverage = self._measure_index_agreement()

    def _measure_index_agreement(self, sample: int = 24,
                                 k: int = 10) -> Dict[str, float]:
        """Per index: how much of the FUSED answer that index alone recovers.

        Each index's `recall` answers "did this operator compute its own
        similarity function faithfully" — an exact vector scan scores 1.0 there
        by definition. That is a different question from "did it find what the
        user wanted", and conflating them made fusion unpickable: nothing can
        beat 1.0, so the planner could never justify consulting a second index
        even on corpora where doing so measurably wins (README, BEIR results).

        The question asked here is the one the planner actually faces: *if I
        skip the second index, how much of the fused result do I lose?* We
        answer it by running the fusion on sample queries and measuring what
        fraction of its top-k each index alone would still have returned.

        An earlier version scored raw disagreement between the two indexes
        (Jaccard) instead. That conflates two opposite situations: the indexes
        can disagree because they are complementary, or because one of them is
        simply wrong. On a corpus where the vector index alone is already
        perfect, a weak lexical index dragged the estimate down and the planner
        started fusing everything — the demo's self-check caught it. Comparing
        against the fused result cannot make that mistake, because an index
        that contributes nothing useful also contributes nothing to the fusion.

        ponytail: one constant per index, not per query. Ceiling: a query whose
        terms are all out-of-vocabulary has worse lexical coverage than the
        corpus mean claims. Upgrade path: condition on query features, which is
        what `LearnedPolicy` does when judgments are available.
        """
        default = {"lexical": 1.0, "vector": 1.0}
        if not (self.lexical and self.vector) or len(self) < 2 * k:
            return default
        vector_field = self.schema.vector_field()
        text_fields = self.schema.text_fields
        if not vector_field or not text_fields:
            return default

        live = [i for i in self._docs if i not in self._deleted]
        step = max(1, len(live) // sample)
        budget = Budget(candidates=k * OVERFETCH, ef=64, k=k)
        recovered = {"lexical": [], "vector": []}
        for internal in live[::step][:sample]:
            doc = self._docs[internal]
            if vector_field not in doc:
                continue
            terms = self.lexical.analyze_query(
                " ".join(str(doc[f]) for f in text_fields if f in doc))
            if not terms:
                continue
            lexical_hits = self.lexical.search(terms, budget)
            vector_hits = self.vector.search(doc[vector_field], budget)
            fused = {d for d, _ in ranking.top_k(
                ranking.rrf([lexical_hits, vector_hits]), k)}
            # The probe document is its own best match everywhere, so counting
            # it would report agreement that no real query enjoys.
            fused.discard(internal)
            if not fused:
                continue
            for name, hits in (("lexical", lexical_hits), ("vector", vector_hits)):
                alone = {d for d, _ in ranking.top_k(hits.scores, k)}
                alone.discard(internal)
                recovered[name].append(len(alone & fused) / len(fused))
        return {name: (sum(v) / len(v) if v else 1.0)
                for name, v in recovered.items()}

    def _measure_pipeline_overhead(self) -> Tuple[float, float]:
        """Per-query cost outside the indexes: planning, query resolution and
        building `Results`. Returns (fixed ms, ms per returned hit).

        Deliberately measured ONCE, not learned from live traffic. An earlier
        version updated it per query from observed latency, which coupled
        queries together — one slow query silently re-priced the next one's
        plan. Constants are identical across candidate plans for a given query,
        so they cannot distort plan CHOICE; they exist so that
        `latency_budget_ms` is compared against an honest number.

        It is a LINE in k, not a constant, because marshalling results into
        `Hit` objects is O(k). Calibrating at a single k and applying it to
        every k under-charged the fixed cost of a k=50 query by ~35%, which was
        the single largest remaining source of cost-model error on small
        queries — there the pipeline IS most of the latency. This is the same
        k-dependence already priced inside the vector index; the pipeline had
        it too and was not modelled.
        """
        if not (self.lexical and self.lexical.postings):
            return 0.0, 0.0
        # The most common term, so the probe can actually RETURN every k below.
        # With an arbitrary term the ladder ran past the number of matching
        # documents, the curve flattened where it should have kept rising, and
        # the fit flipped between "high fixed cost, no per-hit cost" and the
        # reverse depending on noise — a 2.6x swing in a constant that dominates
        # small queries.
        term = max(self.lexical.postings, key=self.lexical.df)
        ladder = [k for k in (5, 20, 50, 100) if k <= self.lexical.df(term)]
        if len(ladder) < 2:
            ladder = [1, max(2, self.lexical.df(term))]
        points = []
        history = self.stats.history
        try:
            # These are synthetic probes, not user traffic. Leaving them in the
            # history would poison the training signal a LearnedPolicy reads.
            self.stats.history = []
            for k in ladder:
                for _ in range(3):  # warm caches before the first timed sample
                    self.search(text=term, k=k, explain=True)
                totals, executions = [], []
                for _ in range(TIMING_REPEATS * 3):
                    ex = self.search(text=term, k=k, explain=True).explain
                    totals.append(ex.actual_latency_ms)
                    executions.append(ex.execution_ms)
                # Minimise each quantity separately, then subtract. Taking
                # min(total - execution) per run subtracts two nearly-equal
                # noisy timers and keeps whichever run had the unluckiest pair,
                # which made this constant swing 2x between calibrations of an
                # identical corpus — the single largest source of cost-model
                # error. Each minimum is individually stable, and total >=
                # execution holds per run, so the difference stays >= 0.
                points.append((float(k), min(totals) - min(executions)))
        finally:
            self.stats.history = history
        base_ms, ms_per_hit = fit_linear(points)   # unit-agnostic: ms in, ms out
        return max(base_ms, 0.0), max(ms_per_hit, 0.0)

    def _filter_calibration_samples(self) -> List[Dict[str, Predicate]]:
        """Build filters whose result sizes SPAN a wide range.

        Sampling one predicate per value gives near-identical cardinalities, and
        a fit with no spread in x cannot separate fixed cost from per-document
        cost — it dumps everything into the slope and then wildly overcharges
        large filters. Growing `OneOf` sets gives the fit real leverage.
        """
        samples: List[Dict[str, Predicate]] = []
        for name in list(self.structured.fields)[:3]:
            values = sorted(self.structured.bitmaps[name],
                            key=lambda v: -len(self.structured.bitmaps[name][v]))[:8]
            for size in range(1, len(values) + 1):
                subset = values[:size]
                samples.append({name: Eq(subset[0]) if size == 1
                                else OneOf(list(subset))})
        return samples

    def _index_stats(self) -> Dict[str, Any]:
        out = {}
        for name in ("lexical", "vector", "structured"):
            engine = getattr(self, name)
            if engine is not None:
                out[name] = engine.statistics()
        return out

    # ----------------------------- searching ------------------------------- #
    def search(self, text: Optional[str] = None,
               semantic: Union[str, Sequence[float], None] = None,
               where: Optional[Dict[str, Any]] = None,
               recent: Optional[str] = None,
               k: int = 10,
               recall: float = 0.9,
               explain: bool = False,
               pin: Optional[str] = None,
               decay: Optional[str] = None,
               rerank: Optional[Callable[[List[int]], Dict[int, float]]] = None,
               ) -> Results:
        """Search by intent. The optimizer picks the plan.

        `pin` forces a plan by name (for benchmarking/debugging only).
        """
        if not self._calibrated and len(self):
            self.calibrate()

        started = time.perf_counter()
        vector = self._resolve_semantic(semantic, text)
        predicates = normalize_where(where)
        if recent:
            predicates = self._apply_recent(predicates, recent)

        query = Query(text=text, semantic=vector, where=predicates,
                      k=max(1, int(k)), recall_target=recall,
                      explain=explain, pin=pin, decay=decay)

        plan, ctx, considered = self.optimizer.plan(query, len(self))
        timestamps = self._timestamps() if decay else None
        results, stages, execution_ms = self.executor.run(
            plan, ctx, timestamps=timestamps, reranker=rerank)

        hits = [Hit(id=self._ext_ids[doc_id], score=float(score),
                    doc=self._docs.get(doc_id, {}))
                for doc_id, score in results]
        # Compare like with like: the estimate covers the whole query including
        # planning and marshalling, so the actual it is scored against must too.
        # Charging the estimate for `pipeline_ms` and then grading it against
        # execution time alone reported an error the model had not made.
        total_ms = (time.perf_counter() - started) * 1000.0

        # Close the loop: estimate vs. actual is logged for every query. This is
        # the training signal a LearnedPolicy will consume (SystemDesign §5).
        self.stats.observe(ctx.features, plan.name, plan.estimate.latency_ms,
                           plan.estimate.recall, total_ms, len(results))
        self.optimizer.policy.observe(plan, ctx, total_ms, len(results))

        payload = Explain(plan=plan, stages=stages, actual_latency_ms=total_ms,
                          execution_ms=execution_ms,
                          considered=considered) if explain else None
        return Results(hits, payload)

    def _resolve_semantic(self, semantic, text) -> Optional[List[float]]:
        """Strings become vectors only if an embedder was configured. If one is,
        a text query also gets a semantic intent for free — and the optimizer
        then decides whether using it is worth the latency."""
        if semantic is None:
            if self.vector is not None and self.embedder and text:
                return list(self.embedder(text))
            return None
        if isinstance(semantic, str):
            if not self.embedder:
                raise ValueError(
                    "semantic=<str> needs an embedder: "
                    "Index.create(..., embedder=fn) or pass a vector")
            return list(self.embedder(semantic))
        return list(semantic)

    def _apply_recent(self, predicates: Dict[str, Predicate],
                      recent: str) -> Dict[str, Predicate]:
        if not self._time_field:
            raise ValueError("recent= requires a Datetime field in the schema")
        cutoff = time.time() - parse_duration(recent)
        predicates = dict(predicates)
        predicates[self._time_field] = Range(low=cutoff)
        return predicates

    def _timestamps(self) -> Dict[int, float]:
        if not self._time_field or not self.structured:
            return {}
        return dict(self.structured.values.get(self._time_field, {}))

    # ------------------------------ reporting ------------------------------ #
    def describe(self) -> Dict[str, Any]:
        return {"path": self.path, "n_docs": len(self),
                "schema": self.schema.spec(), "indexes": self._index_stats()}

    def statistics(self) -> Dict[str, Any]:
        self.stats.n_docs = len(self)
        self.stats.index_stats = self._index_stats()
        return self.stats.summary()
