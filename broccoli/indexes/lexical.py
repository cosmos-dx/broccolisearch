"""Lexical index: analyzer + inverted index + BM25 (Information.md §2).

ponytail: this is a minimal inverted index (token -> {doc: tf}) rather than a
rented Lucene. It exists because the optimizer needs posting lists and document
frequencies to estimate cardinality, and no installed Python package exposes
those (`rank_bm25` scores every document, which defeats the purpose).
Ceiling: no positions/phrase queries, no skip lists, no on-disk segments.
Upgrade path: swap this class for a Tantivy adapter in the Rust port — the
BaseIndex interface is what the optimizer depends on, not this implementation.
"""

from __future__ import annotations

import heapq
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Set

from ..calibration import TIMING_REPEATS, fit_linear
from ..types import Budget, Capabilities, CandidateSet, CostEstimate
from . import BaseIndex

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# The Rust core is optional: it is a build artefact, not a dependency, so the
# library has to run without it. `BROCCOLI_NO_RUST=1` forces the Python path,
# which is what lets the test suite run both and assert they agree.
try:                                                # pragma: no cover
    import broccoli_core as _rust
except ImportError:                                 # pragma: no cover
    _rust = None

RUST = _rust is not None and os.environ.get("BROCCOLI_NO_RUST") != "1"

# "Return everything, trim on the Python side" — used when the caller has to
# filter the result before deciding what the top candidates even are.
_NO_TRIM = 1 << 62


class _RustPostings(Mapping):
    """Read-only `{token: {doc_id: tf}}` view over the Rust core's index.

    The Rust core owns the postings, but calibration picks probe terms by
    iterating the vocabulary and the tests inspect posting lists directly.
    Presenting a Mapping means neither has to know which backend is loaded —
    which is the whole claim in Architecture.md §6, that the port swaps an
    implementation and not an interface.
    """

    def __init__(self, core):
        self._core = core
        self._vocabulary: Optional[List[str]] = None

    def invalidate(self) -> None:
        self._vocabulary = None

    def _tokens(self) -> List[str]:
        if self._vocabulary is None:
            self._vocabulary = self._core.vocabulary()
        return self._vocabulary

    def __getitem__(self, token):
        pairs = self._core.postings(token)
        if not pairs:
            raise KeyError(token)
        return dict(pairs)

    def __iter__(self):
        return iter(self._tokens())

    def __len__(self):
        return len(self._tokens())


def _time_once(fn, *args) -> float:
    started = time.perf_counter()
    fn(*args)
    return time.perf_counter() - started

STOPWORDS = frozenset("""
a an and are as at be but by for if in into is it no not of on or such that the
their then there these they this to was will with from we you your our
""".split())

_SUFFIXES = ("ingly", "edly", "ing", "ies", "ied", "es", "ed", "ly", "s")

# BM25 constants (the standard defaults).
K1 = 1.2
B = 0.75


def analyze(text: str, analyzer: str = "standard") -> List[str]:
    """Lowercase → tokenize → drop stopwords → light stem.

    ponytail: the 'english' analyzer is suffix-stripping, not a real Porter
    stemmer. Ceiling: over/under-stems irregular words. Upgrade path: a proper
    stemmer library, or Tantivy's analyzer in the Rust port.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if analyzer == "raw":
        return tokens
    out = []
    for t in tokens:
        if t in STOPWORDS or len(t) == 1:
            continue
        if analyzer == "english":
            for suf in _SUFFIXES:
                if len(t) > len(suf) + 2 and t.endswith(suf):
                    t = t[: -len(suf)]
                    break
        out.append(t)
    return out


class LexicalIndex(BaseIndex):
    name = "lexical"

    def __init__(self, fields: Dict[str, str], deleted: Optional[Set[int]] = None):
        """`fields` maps text field name -> analyzer name."""
        self.fields = dict(fields)
        self.deleted = deleted if deleted is not None else set()
        # The Rust core owns the postings when it is available; the Python
        # dicts below are the fallback, not a duplicate copy of them.
        self._core = _rust.LexicalCore() if RUST else None
        self.postings: Any = (_RustPostings(self._core) if self._core is not None
                              else {})            # token -> {doc_id: tf}
        self._doc_len: Dict[int, int] = {}
        self._total_len = 0
        # Calibrated at build time: latency = base_ms + postings * sec_per_posting.
        self.sec_per_posting = 2.5e-7
        self.base_ms = 0.01

    # ------------------------------ build ---------------------------------- #
    def _tokens(self, fields: Dict[str, Any]) -> List[str]:
        """Analyze a document into the flat token list both backends index.

        Analysis stays in Python on purpose: it is done once per document
        rather than once per posting, so it is not hot, and a second stemmer
        implementation in Rust could drift out of step with this one. Index-time
        and query-time analysis disagreeing is the classic silent recall bug.
        """
        tokens: List[str] = []
        for name, analyzer in self.fields.items():
            value = fields.get(name)
            if value:
                tokens.extend(analyze(value, analyzer))
        return tokens

    def add(self, doc_id: int, fields: Dict[str, Any]) -> None:
        tokens = self._tokens(fields)
        if self._core is not None:
            self._core.add(doc_id, tokens)
            self.postings.invalidate()
            return
        for token in tokens:
            self.postings.setdefault(token, {})
            self.postings[token][doc_id] = self.postings[token].get(doc_id, 0) + 1
        self._doc_len[doc_id] = len(tokens)
        self._total_len += len(tokens)

    def remove(self, doc_id: int, fields: Optional[Dict[str, Any]] = None) -> None:
        """Drop a document's postings.

        Given the document's fields this touches only the terms it actually
        contains; without them it must sweep the whole vocabulary, which makes
        a single delete cost O(vocabulary) instead of O(document length).
        """
        if self._core is not None:
            self._core.remove(doc_id, self._tokens(fields) if fields else None)
            self.postings.invalidate()
            return
        length = self._doc_len.pop(doc_id, 0)
        self._total_len -= length
        if fields is None:
            for postings in self.postings.values():
                postings.pop(doc_id, None)
            return
        for token in self._tokens(fields):
            postings = self.postings.get(token)
            if postings:
                postings.pop(doc_id, None)

    @property
    def doc_len(self) -> Dict[int, int]:
        if self._core is not None:
            return self._core.doc_lens()
        return self._doc_len

    @property
    def n_docs(self) -> int:
        if self._core is not None:
            return self._core.n_docs()
        return len(self._doc_len)

    @property
    def avgdl(self) -> float:
        total = self._core.total_len() if self._core is not None else self._total_len
        return (total / self.n_docs) if self.n_docs else 0.0

    def df(self, term: str) -> int:
        if self._core is not None:
            return self._core.df(term)
        return len(self.postings.get(term, ()))

    def analyze_query(self, text: str) -> List[str]:
        """Query-time analysis MUST match index-time analysis or recall dies."""
        analyzers = set(self.fields.values()) or {"standard"}
        analyzer = next(iter(analyzers)) if len(analyzers) == 1 else "standard"
        return analyze(text, analyzer)

    # ---------------------------- interface -------------------------------- #
    def capabilities(self) -> Capabilities:
        return Capabilities(lexical=True)

    def estimate(self, terms: Iterable[str], budget: Budget) -> CostEstimate:
        terms = list(terms)
        cap = len(budget.domain) if budget.domain is not None else None
        work = sum(min(self.df(t), cap) if cap is not None else self.df(t)
                   for t in terms)
        # Union cardinality upper bound; cheap and good enough to plan with.
        cardinality = min(work, self.n_docs)
        if budget.domain is not None:
            selectivity = len(budget.domain) / max(self.n_docs, 1)
            cardinality = int(cardinality * selectivity)
        latency_ms = self.base_ms + work * self.sec_per_posting * 1000.0
        # Fidelity: you cannot return k relevant docs if the posting union holds
        # fewer than k docs at all. Compare against the caller's k, NOT the
        # overfetch budget — a term matching exactly the k documents the user
        # wants has full coverage, not 1/overfetch of it.
        #
        # This used to carry a hardcoded 0.92 for "vocabulary mismatch: a
        # lexical index cannot match a synonym it never saw". That was the right
        # idea guessed at, and it belonged to neither this index nor this
        # scale — the vector index needed the same discount and had none, which
        # is why an exact scan could claim a perfect score. It is now measured
        # once per corpus as `Optimizer.solo_coverage` and applied to whichever
        # index is being used alone.
        recall = min(1.0, cardinality / max(budget.k, 1))
        return CostEstimate(latency_ms=latency_ms, recall=recall,
                            cardinality=int(cardinality))

    def _search_native(self, terms: List[str], budget: Budget) -> CandidateSet:
        """BM25 in the Rust core: one FFI crossing for the whole scan, never
        one per document (Architecture.md §1.4).

        The join order is decided HERE rather than in Rust, because handing the
        domain across the boundary costs O(|domain|) whatever Rust then does
        with it. Pushing a 4000-document filter down onto a 50-document posting
        list made the query 80x more expensive than the cost model predicted:
        the model charges min(df, |domain|) and the marshalling silently charged
        |domain| on top. Choosing the side up here keeps the code spending the
        same quantity the model estimates.
        """
        deleted = self.deleted or None
        domain = budget.domain
        if domain is not None and len(domain) >= sum(self.df(t) for t in terms):
            # The posting lists are the smaller side, so scan them whole and
            # drop non-members here: that is df set lookups in Python against
            # |domain| elements marshalled into Rust, and we just established
            # which of those is smaller. Trimming has to happen after the
            # filter, or the surviving documents would be chosen from an
            # already-truncated list.
            ids, values, examined = self._core.search(terms, _NO_TRIM, None, deleted)
            pairs = [kv for kv in zip(ids, values) if kv[0] in domain]
            if len(pairs) > budget.candidates:
                pairs = heapq.nlargest(budget.candidates, pairs,
                                       key=lambda kv: (kv[1], -kv[0]))
            scores = dict(pairs)
        else:
            ids, values, examined = self._core.search(
                terms, budget.candidates, domain, deleted)
            scores = dict(zip(ids, values))
        return CandidateSet(scores=scores, source=self.name, examined=examined)

    def search(self, terms: Iterable[str], budget: Budget) -> CandidateSet:
        """BM25 over the posting lists of the query terms only."""
        started = time.perf_counter()
        terms = list(terms)
        if self._core is not None:
            cs = self._search_native(terms, budget)
            self._last_latency_ms = (time.perf_counter() - started) * 1000.0
            return cs
        scores: Dict[int, float] = {}
        examined = 0
        n = max(self.n_docs, 1)
        avgdl = self.avgdl or 1.0
        domain = budget.domain
        doc_len = self._doc_len          # hoisted: this is read once per posting
        for term in terms:
            postings = self.postings.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            # Iterate whichever side is smaller — the classic join order. A
            # selective filter leaving 50 survivors should not drag a 50k-entry
            # posting list through the interpreter to discard 99.9% of it.
            if domain is not None and len(domain) < df:
                examined += len(domain)
                pairs = ((d, postings[d]) for d in domain if d in postings)
            else:
                examined += df
                pairs = ((d, tf) for d, tf in postings.items()
                         if domain is None or d in domain)
            for doc_id, tf in pairs:
                if doc_id in self.deleted:
                    continue
                dl = doc_len.get(doc_id, 0)
                denom = tf + K1 * (1 - B + B * dl / avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (K1 + 1)) / denom
        if len(scores) > budget.candidates:
            scores = dict(heapq.nlargest(budget.candidates, scores.items(),
                                         key=lambda kv: (kv[1], -kv[0])))
        cs = CandidateSet(scores=scores, source=self.name, examined=examined)
        self._last_latency_ms = (time.perf_counter() - started) * 1000.0
        return cs

    def calibrate(self, sample_terms: List[str]) -> None:
        """Measure this machine's real cost curve for a posting-list scan.

        Samples must span a WIDE range of document frequencies, otherwise the
        fit has no leverage to separate the fixed per-query cost from the
        per-posting cost (Approach.md §3 — calibration is not optional).
        """
        candidates = [t for t in sample_terms if self.df(t)]
        if not candidates:
            return
        by_df = sorted(candidates, key=self.df)
        # Take from both ends of the df range plus the middle.
        picks = by_df[:8] + by_df[len(by_df) // 2: len(by_df) // 2 + 4] + by_df[-8:]
        budget = Budget(candidates=10)
        points = []
        for term in picks:
            # min-of-N: the fastest run is the least contaminated by scheduler
            # noise, so it estimates the true cost better than a mean does.
            best = min(_time_once(self.search, [term], budget)
                       for _ in range(TIMING_REPEATS))
            points.append((float(self.df(term)), best))
        if not points:
            return
        base_s, slope_s = fit_linear(points)
        self.base_ms = base_s * 1000.0
        self.sec_per_posting = slope_s

    def statistics(self) -> Dict[str, Any]:
        return {"n_docs": self.n_docs, "n_terms": len(self.postings),
                "avgdl": self.avgdl, "sec_per_posting": self.sec_per_posting,
                "base_ms": self.base_ms}
