//! Native inverted index + BM25 scan for broccolisearch.
//!
//! This is the first piece of the Rust core described in Architecture.md §6.
//! It ports the one loop that profiling points at — scoring posting lists —
//! and nothing else. In particular the vector
//! index is deliberately NOT here: its exact scan is a single numpy matmul that
//! already runs in BLAS with SIMD, so re-implementing it in Rust would trade a
//! tuned kernel for a hand-written one.
//!
//! Two invariants make this safe to swap in underneath the Python class:
//!
//! 1. **Analysis stays in Python.** Tokenising and stemming are neither hot nor
//!    simple, and two implementations of a stemmer would silently diverge —
//!    query-time and index-time analysis disagreeing is the classic way to
//!    destroy recall. Rust receives tokens, never text.
//! 2. **Arithmetic is bit-identical.** f64 throughout, and per-document scores
//!    accumulate in query-term order exactly as the Python loop does, so the
//!    two paths return the same floats and the test suite can assert equality
//!    rather than approximate agreement.
//!
//! The FFI boundary is coarse (Architecture.md §1.4): one crossing per
//! `search`, never per document.

use pyo3::prelude::*;
use std::collections::{HashMap, HashSet};

const K1: f64 = 1.2;
const B: f64 = 0.75;

/// An inverted index owning its postings, so a query does not have to marshal
/// them across the FFI boundary.
#[pyclass]
struct LexicalCore {
    /// token -> postings, kept sorted by doc id.
    postings: HashMap<String, Vec<(u32, u32)>>,
    doc_len: HashMap<u32, u32>,
    total_len: u64,
}

#[pymethods]
impl LexicalCore {
    #[new]
    fn new() -> Self {
        LexicalCore {
            postings: HashMap::new(),
            doc_len: HashMap::new(),
            total_len: 0,
        }
    }

    /// Index one document from its already-analyzed tokens.
    fn add(&mut self, doc_id: u32, tokens: Vec<String>) {
        for token in &tokens {
            let list = self.postings.entry(token.clone()).or_default();
            // Documents arrive with monotonically increasing ids, so the common
            // path is "same doc as last time" or "append". The binary search is
            // only a correctness guard for out-of-order inserts.
            match list.last_mut() {
                Some(last) if last.0 == doc_id => last.1 += 1,
                Some(last) if last.0 < doc_id => list.push((doc_id, 1)),
                None => list.push((doc_id, 1)),
                _ => match list.binary_search_by_key(&doc_id, |e| e.0) {
                    Ok(i) => list[i].1 += 1,
                    Err(i) => list.insert(i, (doc_id, 1)),
                },
            }
        }
        let length = tokens.len() as u32;
        let previous = self.doc_len.insert(doc_id, length).unwrap_or(0);
        self.total_len = self.total_len + length as u64 - previous as u64;
    }

    /// Drop a document's postings. `tokens` is the document's own token list;
    /// without it every posting list has to be swept, which turns one delete
    /// into O(vocabulary) instead of O(document length).
    #[pyo3(signature = (doc_id, tokens=None))]
    fn remove(&mut self, doc_id: u32, tokens: Option<Vec<String>>) {
        let length = self.doc_len.remove(&doc_id).unwrap_or(0);
        self.total_len -= length as u64;
        match tokens {
            Some(tokens) => {
                for token in tokens {
                    if let Some(list) = self.postings.get_mut(&token) {
                        if let Ok(i) = list.binary_search_by_key(&doc_id, |e| e.0) {
                            list.remove(i);
                        }
                    }
                }
            }
            None => {
                for list in self.postings.values_mut() {
                    if let Ok(i) = list.binary_search_by_key(&doc_id, |e| e.0) {
                        list.remove(i);
                    }
                }
            }
        }
    }

    fn df(&self, token: &str) -> usize {
        self.postings.get(token).map_or(0, |l| l.len())
    }

    fn n_docs(&self) -> usize {
        self.doc_len.len()
    }

    fn total_len(&self) -> u64 {
        self.total_len
    }

    fn vocabulary(&self) -> Vec<String> {
        self.postings.keys().cloned().collect()
    }

    /// One token's postings as `[(doc_id, tf), ...]`. The Python side wraps
    /// this in a read-only Mapping so calibration and tests can inspect the
    /// index the same way whichever backend is in use.
    fn postings(&self, token: &str) -> Vec<(u32, u32)> {
        self.postings.get(token).cloned().unwrap_or_default()
    }

    fn doc_lens(&self) -> HashMap<u32, u32> {
        self.doc_len.clone()
    }

    /// BM25 over the posting lists of the query terms only.
    ///
    /// Returns `(doc_ids, scores, examined)`. `examined` is the work-unit count
    /// the cost model is calibrated against, so it must count the same thing
    /// the Python implementation counts: postings touched, or domain entries
    /// probed when the filter is the smaller side.
    #[pyo3(signature = (terms, candidates, domain=None, deleted=None))]
    fn search(
        &self,
        terms: Vec<String>,
        candidates: usize,
        domain: Option<HashSet<u32>>,
        deleted: Option<HashSet<u32>>,
    ) -> (Vec<u32>, Vec<f64>, usize) {
        let n = self.doc_len.len().max(1) as f64;
        let avgdl = if self.doc_len.is_empty() {
            1.0
        } else {
            let a = self.total_len as f64 / self.doc_len.len() as f64;
            if a == 0.0 {
                1.0
            } else {
                a
            }
        };
        let deleted = deleted.unwrap_or_default();

        let mut scores: HashMap<u32, f64> = HashMap::new();
        let mut order: Vec<u32> = Vec::new(); // first-seen order, to mirror Python dicts
        let mut examined: usize = 0;

        for term in &terms {
            let list = match self.postings.get(term) {
                Some(l) if !l.is_empty() => l,
                _ => continue,
            };
            let df = list.len() as f64;
            let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();

            // Iterate whichever side is smaller — the classic join order. A
            // selective filter leaving 50 survivors must not drag a 50k-entry
            // posting list through memory to discard 99.9% of it.
            let use_domain = match &domain {
                Some(d) => d.len() < list.len(),
                None => false,
            };
            if use_domain {
                let d = domain.as_ref().unwrap();
                examined += d.len();
                for &doc_id in d {
                    if let Ok(i) = list.binary_search_by_key(&doc_id, |e| e.0) {
                        Self::accumulate(
                            &mut scores, &mut order, &deleted, &self.doc_len,
                            doc_id, list[i].1, idf, avgdl,
                        );
                    }
                }
            } else {
                examined += list.len();
                for &(doc_id, tf) in list.iter() {
                    if let Some(d) = &domain {
                        if !d.contains(&doc_id) {
                            continue;
                        }
                    }
                    Self::accumulate(
                        &mut scores, &mut order, &deleted, &self.doc_len,
                        doc_id, tf, idf, avgdl,
                    );
                }
            }
        }

        // Trim to the candidate budget, ties broken by doc id, matching
        // `heapq.nlargest(key=(score, -doc_id))`.
        let mut items: Vec<(u32, f64)> =
            order.into_iter().map(|d| (d, scores[&d])).collect();
        if items.len() > candidates {
            items.sort_by(|a, b| {
                b.1.partial_cmp(&a.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(a.0.cmp(&b.0))
            });
            items.truncate(candidates);
        }
        let ids = items.iter().map(|e| e.0).collect();
        let out = items.iter().map(|e| e.1).collect();
        (ids, out, examined)
    }
}

impl LexicalCore {
    #[allow(clippy::too_many_arguments)]
    fn accumulate(
        scores: &mut HashMap<u32, f64>,
        order: &mut Vec<u32>,
        deleted: &HashSet<u32>,
        doc_len: &HashMap<u32, u32>,
        doc_id: u32,
        tf: u32,
        idf: f64,
        avgdl: f64,
    ) {
        if deleted.contains(&doc_id) {
            return;
        }
        let dl = *doc_len.get(&doc_id).unwrap_or(&0) as f64;
        let tf = tf as f64;
        let denom = tf + K1 * (1.0 - B + B * dl / avgdl);
        let contribution = idf * (tf * (K1 + 1.0)) / denom;
        match scores.get_mut(&doc_id) {
            Some(existing) => *existing += contribution,
            None => {
                scores.insert(doc_id, contribution);
                order.push(doc_id);
            }
        }
    }
}

#[pymodule]
fn broccoli_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<LexicalCore>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
