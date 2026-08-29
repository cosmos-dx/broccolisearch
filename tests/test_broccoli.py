"""Test suite for BroccoliSearch.

Focus is on the logic that would silently produce WRONG results if it broke:
analyzer symmetry, filter exactness, filter push-down, plan selection, fusion,
tombstones, and persistence. Plus the metrics, since every quality claim in the
project rests on them.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pytest

import broccoli
from broccoli.indexes.lexical import analyze
from broccoli.optimizer import QueryContext, RuleBasedPolicy
from broccoli.query import Eq, Plan, PlanEstimate, Query
from broccoli.types import Budget, CandidateSet

DIM = 16
N_CONCEPTS = 6
DOCS_PER_CONCEPT = 25
CATEGORIES = ["tools", "seeds", "books"]


# ------------------------------- fixtures ---------------------------------- #


@pytest.fixture(scope="module")
def corpus():
    """Deterministic corpus with latent concepts so semantic search is meaningful."""
    rng = np.random.default_rng(7)
    centroids = rng.normal(size=(N_CONCEPTS, DIM))
    docs = []
    for c in range(N_CONCEPTS):
        for i in range(DOCS_PER_CONCEPT):
            doc_id = f"c{c}d{i}"
            docs.append({
                "id": doc_id,
                "title": f"concept{c} item {i} gardening",
                "body": f"a document about topic{c} with filler text number {i}",
                "embedding": list(centroids[c] + rng.normal(scale=0.12, size=DIM)),
                "price": float(i),
                "category": CATEGORIES[i % len(CATEGORIES)],
                "concept": c,
            })
    return centroids, docs


@pytest.fixture(scope="module")
def index(corpus):
    centroids, docs = corpus
    idx = broccoli.Index.create(schema={
        "title": broccoli.Text(analyzer="english"),
        "body": broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=DIM, metric="cosine"),
        "price": broccoli.Float(),
        "category": broccoli.Keyword(),
        "concept": broccoli.Int(),
    })
    for doc in docs:
        idx.add(doc)
    idx.calibrate()
    return idx


# ------------------------------- schema ------------------------------------ #


def test_schema_rejects_unknown_field():
    idx = broccoli.Index.create(schema={"title": broccoli.Text()})
    with pytest.raises(KeyError):
        idx.add({"id": "1", "nope": "x"})


def test_schema_rejects_wrong_type():
    idx = broccoli.Index.create(schema={"n": broccoli.Int()})
    with pytest.raises(TypeError):
        idx.add({"id": "1", "n": "not an int"})


def test_schema_rejects_wrong_vector_dim():
    idx = broccoli.Index.create(schema={"v": broccoli.Vector(dim=4)})
    with pytest.raises(ValueError):
        idx.add({"id": "1", "v": [0.1, 0.2]})


def test_document_requires_id():
    idx = broccoli.Index.create(schema={"title": broccoli.Text()})
    with pytest.raises(ValueError):
        idx.add({"title": "no id here"})


# ------------------------------- analyzer ---------------------------------- #


def test_analyzer_is_symmetric_between_index_and_query(index):
    """The #1 cause of silent recall loss: index-time and query-time analysis
    disagreeing. 'gardening' must stem to whatever the index stored."""
    indexed = analyze("item gardening", "english")
    queried = index.lexical.analyze_query("GARDENING items")
    assert set(indexed) & set(queried), (indexed, queried)


def test_analyzer_drops_stopwords():
    assert "the" not in analyze("the quick brown fox", "english")


# ------------------------------- lexical ----------------------------------- #


def test_lexical_finds_the_matching_concept(index):
    hits = index.search(text="concept3", k=10)
    assert hits, "expected lexical matches"
    assert all(h.doc["concept"] == 3 for h in hits)


def test_document_frequency_matches_postings(index):
    terms = index.lexical.analyze_query("concept0")
    for t in terms:
        assert index.lexical.df(t) == len(index.lexical.postings.get(t, {}))


# ------------------------------ structured --------------------------------- #


def test_structured_filter_is_exact(index):
    hits = index.search(text="gardening", where={"category": "seeds"}, k=50)
    assert hits
    assert all(h.doc["category"] == "seeds" for h in hits)


def test_range_filter_bounds(index):
    hits = index.search(text="gardening", where={"price": broccoli.lt(5)}, k=100)
    assert hits
    assert all(h.doc["price"] < 5 for h in hits)


def test_between_filter_is_inclusive(index):
    hits = index.search(text="gardening", where={"price": broccoli.between(3, 5)},
                        k=100)
    assert hits
    assert all(3 <= h.doc["price"] <= 5 for h in hits)


def test_one_of_filter(index):
    hits = index.search(text="gardening",
                        where={"category": broccoli.one_of("seeds", "books")}, k=100)
    assert hits
    assert all(h.doc["category"] in ("seeds", "books") for h in hits)


def test_combined_filters_intersect(index):
    hits = index.search(text="gardening",
                        where={"category": "seeds", "price": broccoli.lt(10)}, k=100)
    assert hits
    assert all(h.doc["category"] == "seeds" and h.doc["price"] < 10 for h in hits)


# -------------------------------- vector ----------------------------------- #


def test_vector_search_finds_its_own_concept(index, corpus):
    centroids, _ = corpus
    hits = index.search(semantic=list(centroids[2]), k=10)
    assert hits
    majority = sum(1 for h in hits if h.doc["concept"] == 2)
    assert majority >= 8, f"expected concept 2 to dominate, got {majority}/10"


def test_vector_respects_pushed_down_filter(index, corpus):
    """Filter push-down must be a hard constraint, not a ranking hint."""
    centroids, _ = corpus
    hits = index.search(semantic=list(centroids[1]), where={"category": "books"}, k=20)
    assert hits
    assert all(h.doc["category"] == "books" for h in hits)


def test_selective_filter_makes_vector_search_exact(index, corpus):
    """A small survivor set flips the vector index from approximate to exact —
    cheaper AND recall 1.0. This is the core cost-based decision."""
    centroids, _ = corpus
    small_domain = set(list(range(50)))
    est = index.vector.estimate(list(centroids[0]),
                                Budget(candidates=10, ef=64, domain=small_domain))
    assert est.recall == 1.0
    assert index.vector._mode(Budget(candidates=10, ef=64,
                                     domain=small_domain)) == "exact"


def test_calibration_produces_a_real_curve(index):
    """An uncalibrated cost model is confidently wrong."""
    curve = index.vector.curve
    assert curve, "expected a measured recall/latency curve"
    for ef, point in curve.items():
        assert 0.0 <= point["recall"] <= 1.0
        assert point["latency_ms"] > 0
    # Recall must not get worse as we spend more ef.
    efs = sorted(curve)
    recalls = [curve[e]["recall"] for e in efs]
    assert recalls[-1] >= recalls[0] - 1e-9


# ----------------------------- cost model ---------------------------------- #


def test_fit_linear_recovers_a_known_line():
    from broccoli.calibration import fit_linear
    points = [(x, 2.0 + 3.0 * x) for x in (1.0, 2.0, 5.0, 10.0)]
    base, slope = fit_linear(points)
    assert base == pytest.approx(2.0, abs=1e-6)
    assert slope == pytest.approx(3.0, abs=1e-6)


def test_fit_linear_never_returns_negative_cost():
    """A negative slope would make the optimizer prefer plans that cost less
    the more work they do."""
    from broccoli.calibration import fit_linear
    base, slope = fit_linear([(1.0, 10.0), (10.0, 1.0)])
    assert base >= 0.0 and slope > 0.0


def test_fit_linear_handles_degenerate_input():
    from broccoli.calibration import fit_linear
    assert fit_linear([]) == (0.0, pytest.approx(1e-10))
    base, slope = fit_linear([(5.0, 1.0), (5.0, 1.0)])  # no spread in x
    assert base >= 0.0 and slope > 0.0


def test_lexical_cost_is_monotone_in_posting_work(index):
    """The cost model must rank plans correctly: scanning more postings can
    never be estimated as cheaper."""
    rare = min(index.lexical.postings, key=index.lexical.df)
    common = max(index.lexical.postings, key=index.lexical.df)
    budget = Budget(candidates=50)
    assert (index.lexical.estimate([common], budget).latency_ms
            >= index.lexical.estimate([rare], budget).latency_ms)


def test_filter_cost_grows_with_result_cardinality(index):
    from broccoli.query import Eq, OneOf
    budget = Budget(candidates=50)
    narrow = index.structured.estimate({"category": Eq("seeds")}, budget)
    wide = index.structured.estimate(
        {"category": OneOf(["seeds", "books", "tools"])}, budget)
    assert wide.cardinality >= narrow.cardinality
    assert wide.latency_ms >= narrow.latency_ms


def test_estimates_are_deterministic(index):
    """Identical queries must estimate identically. A shared mutable cost term
    would make query N depend on query N-1 — silently non-reproducible plans."""
    first = index.search(text="concept2", k=5, explain=True)
    second = index.search(text="concept2", k=5, explain=True)
    assert (first.explain.plan.estimate.latency_ms
            == second.explain.plan.estimate.latency_ms)


def test_calibration_separates_fixed_from_marginal_cost(index):
    assert index.lexical.base_ms >= 0.0
    assert index.lexical.sec_per_posting > 0.0
    assert index.vector.base_ms >= 0.0
    assert index.structured.base_ms >= 0.0


def test_work_units_are_reported_for_every_stage(index, corpus):
    """Work units are the deterministic cost metric the evaluation relies on;
    a stage reporting zero would silently make a plan look free."""
    centroids, _ = corpus
    hits = index.search(text="concept3", semantic=list(centroids[3]),
                        where={"category": "seeds"}, k=5, explain=True)
    for stage in hits.explain.stages:
        assert stage.examined >= 0
    assert sum(s.examined for s in hits.explain.stages) > 0


def test_ann_work_scales_with_ef(index, corpus):
    """ANN work must grow with the recall dial, and must be counted in the same
    unit as an exact scan (distance computations), not raw `ef`."""
    centroids, _ = corpus
    cheap = index.vector.search(list(centroids[0]), Budget(candidates=10, ef=16))
    dear = index.vector.search(list(centroids[0]), Budget(candidates=10, ef=256))
    assert dear.examined > cheap.examined
    assert cheap.examined > 16, "ef alone under-counts HNSW distance computations"


def test_estimate_is_graded_against_end_to_end_latency(index, corpus):
    """The estimate covers the whole query (it includes `pipeline_ms`), so the
    'actual' it is compared against must too. Grading it against execution time
    alone reported an error the cost model had not actually made."""
    centroids, _ = corpus
    res = index.search(text="concept1", semantic=list(centroids[1]), k=5,
                       explain=True)
    assert res.explain.execution_ms > 0.0
    assert res.explain.actual_latency_ms >= res.explain.execution_ms
    logged = index.stats.history[-1]
    assert logged.actual_latency_ms == pytest.approx(
        res.explain.actual_latency_ms, rel=1e-9)


def test_filter_survivors_are_not_charged_as_rankable_candidates(index):
    """A filter feeds a retrieval stage; it does not emit candidates to rank.
    Counting its survivors as rankable inflated every filtered plan's cost and
    biased the optimizer against the push-down it is supposed to prefer."""
    wide = index.search(text="concept1", where={"category": "seeds"}, k=5,
                        explain=True)
    ranked = [s for s in wide.explain.stages if s.op == "rank"][0]
    filtered = [s for s in wide.explain.stages if s.op == "filter"][0]
    assert ranked.candidates_in < filtered.candidates_out

    # And the estimate must agree: growing the survivor count while the
    # retrieval stage still emits the same candidates must not move the cost.
    query = Query(text="concept1", where={"category": Eq("seeds")}, k=5)
    ctx = index.optimizer.featurize(query, len(index))
    plan = [p for p in index.optimizer.enumerate_plans(ctx)
            if p.name == "lexical"][0]
    base = index.optimizer.estimate_plan(plan, ctx).latency_ms
    ctx.domain_size = 1_000_000
    assert index.optimizer.estimate_plan(plan, ctx).latency_ms == \
        pytest.approx(base, rel=1e-9)


def test_lexical_join_order_does_not_change_scores(index):
    """`search` scans whichever of (posting list, domain) is smaller. Both
    directions must produce byte-identical scores, or push-down would silently
    change relevance instead of just making it faster."""
    term = max(index.lexical.postings, key=index.lexical.df)
    everything = set(index.lexical.doc_len)
    tiny = set(sorted(index.lexical.postings[term])[:3])

    # Same domain, reached by the two different iteration orders.
    from_postings = index.lexical.search(
        [term], Budget(candidates=500, domain=everything)).scores
    both = {d: s for d, s in from_postings.items() if d in tiny}
    from_domain = index.lexical.search(
        [term], Budget(candidates=500, domain=tiny)).scores
    assert from_domain == both
    assert len(tiny) < index.lexical.df(term), "test needs the domain to be smaller"


def test_lexical_remove_with_fields_matches_full_sweep(corpus):
    """The fast delete path only visits the terms a document contains; it must
    leave the index in exactly the state the exhaustive sweep would."""
    from broccoli.indexes.lexical import LexicalIndex
    _, docs = corpus
    fields = {"title": "english", "body": "english"}
    fast, slow = LexicalIndex(fields), LexicalIndex(fields)
    for i, doc in enumerate(docs[:40]):
        fast.add(i, doc)
        slow.add(i, doc)

    fast.remove(7, docs[7])
    slow.remove(7)
    assert {t: p for t, p in fast.postings.items() if p} == \
           {t: p for t, p in slow.postings.items() if p}
    assert fast.doc_len == slow.doc_len


def test_top_k_matches_a_full_sort_including_ties():
    """nlargest replaced a full sort; ties must still break by doc id."""
    from broccoli import ranking
    scores = {5: 1.0, 3: 1.0, 9: 1.0, 1: 2.0, 7: 0.5}
    reference = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    for k in range(1, len(scores) + 2):
        assert ranking.top_k(scores, k) == reference[:k]


def test_vector_rows_for_ignores_unknown_and_deleted_ids(index):
    """The vectorised domain lookup must drop ids this index never saw and ids
    that are tombstoned — a stray row would score a deleted document."""
    known = list(index.vector._row_of)[:5]
    budget = Budget(candidates=10, domain=set(known) | {10 ** 9})
    rows = index.vector._rows_for(budget)
    assert sorted(rows.tolist()) == sorted(index.vector._row_of[d] for d in known)


def test_fit_linear_survives_a_descheduled_sample():
    """Timing noise is one-sided: a sample can only ever come back too SLOW.

    Least squares let a single such outlier drag the slope far enough that
    calibrated constants moved by orders of magnitude between identical runs.
    """
    from broccoli.calibration import fit_linear
    clean = [(x, 2.0 + 3.0 * x) for x in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)]
    base, slope = fit_linear(clean + [(4.0, 500.0)])
    assert slope == pytest.approx(3.0, abs=0.2)
    assert base == pytest.approx(2.0, abs=1.0)


def test_vector_estimate_prices_the_ef_that_will_actually_run(index, corpus):
    """`_search_ann` widens ef to fit the candidate budget, so an estimate made
    at the requested ef would under-price what the executor really does."""
    centroids, _ = corpus
    vec = list(centroids[0])
    small = index.vector.estimate(vec, Budget(candidates=10, ef=16, k=10))
    large = index.vector.estimate(vec, Budget(candidates=500, ef=16, k=10))
    assert large.latency_ms > small.latency_ms


def test_ann_curve_extrapolates_beyond_the_calibrated_ladder(index):
    """Clamping to the widest measured ef made large budgets look free."""
    widest = max(index.vector.curve)
    at_edge = index.vector._curve_at(widest)["latency_ms"]
    beyond = index.vector._curve_at(widest * 4)["latency_ms"]
    assert beyond > at_edge


def test_selective_filter_is_scanned_exactly_not_walked(index, corpus):
    """A filter that leaves few survivors should be scanned exhaustively: that
    is both cheaper AND exact. The choice must come from comparing costs, not
    from a hardcoded size threshold."""
    domain = set(list(index.vector._row_of)[:5])
    budget = Budget(candidates=50, ef=64, domain=domain, k=10)
    assert index.vector._mode(budget) == "exact"
    assert index.vector.estimate(list(corpus[0][0]), budget).recall == 1.0


def test_pipeline_overhead_does_not_change_plan_ranking(index, corpus):
    """The fixed per-query cost is identical across candidate plans, so it must
    shift every estimate equally and never flip which plan wins."""
    centroids, _ = corpus
    query = Query(text="concept1", semantic=list(centroids[1]), k=5)
    index.optimizer.pipeline_ms = 0.0
    without = [(p.name, p.estimate.latency_ms)
               for p in index.optimizer.enumerate_plans(
                   index.optimizer.featurize(query, len(index)))]
    index.optimizer.pipeline_ms = 5.0
    with_ = [(p.name, p.estimate.latency_ms)
             for p in index.optimizer.enumerate_plans(
                 index.optimizer.featurize(query, len(index)))]
    assert [n for n, _ in without] == [n for n, _ in with_]
    for (_, a), (_, b) in zip(without, with_):
        assert b - a == pytest.approx(5.0, abs=1e-9)


# ------------------------------ optimizer ---------------------------------- #


def _plan(name, latency, recall):
    return Plan(name=name, estimate=PlanEstimate(latency_ms=latency, recall=recall))


def test_policy_picks_cheapest_plan_meeting_target():
    policy = RuleBasedPolicy()
    ctx = QueryContext(query=Query(recall_target=0.9))
    plans = [_plan("slow_good", 100.0, 0.95),
             _plan("fast_bad", 1.0, 0.30),
             _plan("fast_good", 10.0, 0.91)]
    assert policy.choose(plans, ctx).name == "fast_good"


def test_policy_falls_back_to_best_recall_when_target_unreachable():
    policy = RuleBasedPolicy()
    ctx = QueryContext(query=Query(recall_target=0.99))
    plans = [_plan("a", 5.0, 0.50), _plan("b", 50.0, 0.80)]
    assert policy.choose(plans, ctx).name == "b"


def test_optimizer_enumerates_all_three_plans_for_hybrid_intent(index, corpus):
    centroids, _ = corpus
    hits = index.search(text="concept1", semantic=list(centroids[1]), k=5,
                        explain=True)
    names = " ".join(hits.explain.considered)
    assert "lexical" in names and "vector" in names and "hybrid_rrf" in names


def test_optimizer_uses_only_available_intent(index):
    hits = index.search(text="concept1", k=5, explain=True)
    assert hits.explain.plan.name == "lexical"


def test_optimizer_rides_the_ef_curve(index, corpus):
    """The chosen ef should be the SMALLEST that meets the recall target, not a
    hardcoded default."""
    centroids, _ = corpus
    low = index.search(semantic=list(centroids[0]), k=5, recall=0.5, explain=True)
    high = index.search(semantic=list(centroids[0]), k=5, recall=0.99, explain=True)
    ef_low = next(s.budget.ef for s in low.explain.plan.steps if s.op == "vector")
    ef_high = next(s.budget.ef for s in high.explain.plan.steps if s.op == "vector")
    assert ef_low <= ef_high


def test_pin_forces_a_strategy(index, corpus):
    centroids, _ = corpus
    hits = index.search(text="concept1", semantic=list(centroids[1]), k=5,
                        pin="lexical", explain=True)
    assert hits.explain.plan.name == "lexical"


def test_pin_rejects_unavailable_plan(index):
    with pytest.raises(ValueError):
        index.search(text="concept1", pin="vector")


def test_query_without_intent_is_rejected(index):
    with pytest.raises(ValueError):
        index.search(k=5)


# -------------------------------- explain ---------------------------------- #


def test_explain_reports_stages_and_actual_cost(index, corpus):
    centroids, _ = corpus
    hits = index.search(text="concept4", semantic=list(centroids[4]),
                        where={"category": "seeds"}, k=5, explain=True)
    ex = hits.explain
    assert ex.actual_latency_ms > 0
    assert ex.plan.estimate.latency_ms > 0
    ops = [s.op for s in ex.stages]
    assert "filter" in ops and "rank" in ops
    assert "filter" in ex.describe()


def test_history_records_every_query(index):
    before = len(index.stats.history)
    index.search(text="concept2", k=3)
    assert len(index.stats.history) == before + 1
    assert index.stats.history[-1].plan


# -------------------------------- fusion ----------------------------------- #


def test_rrf_rewards_agreement_across_lists():
    """A doc ranked well by BOTH indexes must beat one ranked top by only one."""
    a = CandidateSet(scores={1: 9.0, 2: 8.0, 3: 7.0}, source="lexical")
    b = CandidateSet(scores={2: 0.9, 3: 0.8, 4: 0.7}, source="vector")
    fused = broccoli_rrf([a, b])
    assert fused[2] > fused[1], "doc in both lists should outrank a single-list doc"


def broccoli_rrf(sets):
    from broccoli import ranking
    return ranking.rrf(sets)


def test_normalize_handles_constant_scores():
    from broccoli import ranking
    assert ranking.normalize({1: 5.0, 2: 5.0}) == {1: 1.0, 2: 1.0}
    assert ranking.normalize({}) == {}


def test_top_k_is_deterministic_on_ties():
    from broccoli import ranking
    scores = {3: 1.0, 1: 1.0, 2: 1.0}
    assert ranking.top_k(scores, 3) == [(1, 1.0), (2, 1.0), (3, 1.0)]


# ------------------------------ mutations ---------------------------------- #


def test_deleted_documents_never_surface():
    idx = broccoli.Index.create(schema={"title": broccoli.Text()})
    idx.add({"id": "keep", "title": "broccoli soup"})
    idx.add({"id": "drop", "title": "broccoli soup"})
    assert idx.delete("drop") is True
    ids = idx.search(text="broccoli soup", k=10).ids
    assert ids == ["keep"]
    assert len(idx) == 1


def test_readding_same_id_updates_rather_than_duplicates():
    idx = broccoli.Index.create(schema={"title": broccoli.Text()})
    idx.add({"id": "x", "title": "carrot"})
    idx.add({"id": "x", "title": "broccoli"})
    assert len(idx) == 1
    assert idx.search(text="broccoli", k=5).ids == ["x"]
    assert idx.search(text="carrot", k=5).ids == []


# ----------------------------- persistence --------------------------------- #


def test_save_and_open_round_trip(tmp_path, corpus):
    centroids, docs = corpus
    path = os.path.join(str(tmp_path), "round.broccoli")
    idx = broccoli.Index.create(path, schema={
        "title": broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=DIM),
        "category": broccoli.Keyword(),
    })
    for doc in docs[:60]:
        idx.add({"id": doc["id"], "title": doc["title"],
                 "embedding": doc["embedding"], "category": doc["category"]})
    before = idx.search(text="concept0", k=5).ids
    idx.commit()

    reopened = broccoli.Index.open(path)
    assert len(reopened) == 60
    assert reopened.search(text="concept0", k=5).ids == before


def test_create_refuses_to_clobber(tmp_path):
    path = os.path.join(str(tmp_path), "exists.broccoli")
    broccoli.Index.create(path, schema={"t": broccoli.Text()}).commit()
    with pytest.raises(FileExistsError):
        broccoli.Index.create(path, schema={"t": broccoli.Text()})


# ------------------------------- metrics ----------------------------------- #


def test_recall_at_k():
    assert broccoli.recall_at_k(["a", "b", "c"], {"a", "d"}, 3) == 0.5
    assert broccoli.recall_at_k(["a"], set(), 3) == 1.0


def test_precision_at_k():
    assert broccoli.precision_at_k(["a", "b"], {"a"}, 2) == 0.5


def test_mrr_uses_first_relevant_rank():
    assert broccoli.mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
    assert broccoli.mrr(["x"], {"a"}) == 0.0


def test_ndcg_is_one_for_perfect_ranking():
    rel = {"a": 3.0, "b": 2.0, "c": 1.0}
    assert broccoli.ndcg_at_k(["a", "b", "c"], rel, 3) == pytest.approx(1.0)


def test_ndcg_penalises_bad_ordering():
    rel = {"a": 3.0, "b": 2.0, "c": 1.0}
    good = broccoli.ndcg_at_k(["a", "b", "c"], rel, 3)
    bad = broccoli.ndcg_at_k(["c", "b", "a"], rel, 3)
    assert bad < good


# ------------------------------- harness ----------------------------------- #


def test_harness_compares_optimizer_against_fixed_strategies(index, corpus):
    centroids, docs = corpus
    judgments = []
    for c in range(N_CONCEPTS):
        relevant = {d["id"]: 1.0 for d in docs if d["concept"] == c}
        judgments.append(broccoli.Judgment(
            query={"text": f"concept{c}", "semantic": list(centroids[c])},
            relevant=relevant))
    harness = broccoli.Harness(index, judgments, k=10, recall_target=0.05)
    reports = harness.compare()
    names = {r.name for r in reports}
    assert "ADAPTIVE (optimizer)" in names
    for r in reports:
        assert 0.0 <= r.recall <= 1.0
        assert 0.0 <= r.ndcg <= 1.0
    assert isinstance(harness.report(), str)


def test_latency_at_fixed_recall_picks_cheapest_qualifier():
    from broccoli.eval import StrategyReport
    reports = [
        StrategyReport("slow_ok", 0.95, 0.9, 0.9, 50.0, 60.0, True),
        StrategyReport("fast_bad", 0.10, 0.1, 0.1, 1.0, 2.0, False),
        StrategyReport("fast_ok", 0.91, 0.9, 0.9, 5.0, 6.0, True),
    ]
    best = broccoli.latency_at_fixed_recall(reports, 0.9)
    assert best.name == "fast_ok"


# ------------------------------- duration ---------------------------------- #


def test_duration_parsing():
    from broccoli.query import parse_duration
    assert parse_duration("30d") == 30 * 86400
    assert parse_duration("12h") == 12 * 3600
    with pytest.raises(ValueError):
        parse_duration("soon")
