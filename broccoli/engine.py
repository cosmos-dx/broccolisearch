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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from . import ranking
from .execution import Executor
from .indexes import LexicalIndex, StructuredIndex, VectorIndex
from .optimizer import Optimizer, Policy, RuleBasedPolicy
from .query import (Eq, Explain, Hit, OneOf, Predicate, Query, Range, Results,
                    normalize_where, parse_duration)
from .schema import STRUCTURED_KINDS, Schema, to_timestamp
from .stats import StatisticsStore

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
            self.lexical.remove(internal)
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
        self.optimizer.pipeline_ms = self._measure_pipeline_overhead()

    def _measure_pipeline_overhead(self) -> float:
        """Fixed per-query cost outside the indexes: planning, query resolution
        and building `Results`.

        This is deliberately a constant measured ONCE, not a value learned from
        live traffic. An earlier version updated it per query from observed
        latency, which coupled queries together — one slow query silently
        re-priced the next one's plan. A constant is identical across candidate
        plans, so it cannot distort plan CHOICE; it exists so that
        `latency_budget_ms` is compared against an honest number.
        """
        term = next(iter(self.lexical.postings), None) if self.lexical else None
        if term is None:
            return 0.0
        samples = []
        for _ in range(9):
            started = time.perf_counter()
            results = self.search(text=term, k=10, explain=True)
            wall = (time.perf_counter() - started) * 1000.0
            samples.append(max(wall - results.explain.actual_latency_ms, 0.0))
        samples.sort()
        return samples[len(samples) // 2]

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

        vector = self._resolve_semantic(semantic, text)
        predicates = normalize_where(where)
        if recent:
            predicates = self._apply_recent(predicates, recent)

        query = Query(text=text, semantic=vector, where=predicates,
                      k=max(1, int(k)), recall_target=recall,
                      explain=explain, pin=pin, decay=decay)

        plan, ctx, considered = self.optimizer.plan(query, len(self))
        timestamps = self._timestamps() if decay else None
        results, stages, actual_ms = self.executor.run(
            plan, ctx, timestamps=timestamps, reranker=rerank)

        # Close the loop: estimate vs. actual is logged for every query. This is
        # the training signal a LearnedPolicy will consume (SystemDesign §5).
        self.stats.observe(ctx.features, plan.name, plan.estimate.latency_ms,
                           plan.estimate.recall, actual_ms, len(results))
        self.optimizer.policy.observe(plan, ctx, actual_ms, len(results))

        hits = [Hit(id=self._ext_ids[doc_id], score=float(score),
                    doc=self._docs.get(doc_id, {}))
                for doc_id, score in results]
        payload = Explain(plan=plan, stages=stages, actual_latency_ms=actual_ms,
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
