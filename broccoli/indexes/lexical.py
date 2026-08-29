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

import math
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from ..calibration import TIMING_REPEATS, fit_linear
from ..types import Budget, Capabilities, CandidateSet, CostEstimate
from . import BaseIndex

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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
        self.postings: Dict[str, Dict[int, int]] = {}   # token -> {doc_id: tf}
        self.doc_len: Dict[int, int] = {}
        self._total_len = 0
        # Calibrated at build time: latency = base_ms + postings * sec_per_posting.
        self.sec_per_posting = 2.5e-7
        self.base_ms = 0.01

    # ------------------------------ build ---------------------------------- #
    def add(self, doc_id: int, fields: Dict[str, Any]) -> None:
        length = 0
        for name, analyzer in self.fields.items():
            value = fields.get(name)
            if not value:
                continue
            for token in analyze(value, analyzer):
                self.postings.setdefault(token, {})
                self.postings[token][doc_id] = self.postings[token].get(doc_id, 0) + 1
                length += 1
        self.doc_len[doc_id] = length
        self._total_len += length

    def remove(self, doc_id: int) -> None:
        length = self.doc_len.pop(doc_id, 0)
        self._total_len -= length
        for postings in self.postings.values():
            postings.pop(doc_id, None)

    @property
    def n_docs(self) -> int:
        return len(self.doc_len)

    @property
    def avgdl(self) -> float:
        return (self._total_len / self.n_docs) if self.n_docs else 0.0

    def df(self, term: str) -> int:
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
        work = sum(self.df(t) for t in terms)
        # Union cardinality upper bound; cheap and good enough to plan with.
        cardinality = min(work, self.n_docs)
        if budget.domain is not None:
            selectivity = len(budget.domain) / max(self.n_docs, 1)
            cardinality = int(cardinality * selectivity)
        latency_ms = self.base_ms + work * self.sec_per_posting * 1000.0
        # Heuristic recall: you cannot return k relevant docs if the posting
        # union holds fewer than k docs at all. Compare against the caller's k,
        # NOT the overfetch budget — a term matching exactly the k documents the
        # user wants has full coverage, not 1/overfetch of it. The remaining cap
        # is vocabulary mismatch: a lexical index cannot match a synonym it
        # never saw.
        coverage = min(1.0, cardinality / max(budget.k, 1))
        recall = 0.92 * coverage
        return CostEstimate(latency_ms=latency_ms, recall=recall,
                            cardinality=int(cardinality))

    def search(self, terms: Iterable[str], budget: Budget) -> CandidateSet:
        """BM25 over the posting lists of the query terms only."""
        started = time.perf_counter()
        terms = list(terms)
        scores: Dict[int, float] = {}
        examined = 0
        n = max(self.n_docs, 1)
        avgdl = self.avgdl or 1.0
        for term in terms:
            postings = self.postings.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for doc_id, tf in postings.items():
                examined += 1
                if doc_id in self.deleted:
                    continue
                if budget.domain is not None and doc_id not in budget.domain:
                    continue
                dl = self.doc_len.get(doc_id, 0)
                denom = tf + K1 * (1 - B + B * dl / avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (K1 + 1)) / denom
        if len(scores) > budget.candidates:
            keep = sorted(scores.items(), key=lambda kv: kv[1],
                          reverse=True)[: budget.candidates]
            scores = dict(keep)
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
