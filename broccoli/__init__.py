"""BroccoliSearch — search with a cost-based query optimizer.

    import broccoli

    idx = broccoli.Index.create("./products.broccoli", schema={
        "title":     broccoli.Text(analyzer="english"),
        "embedding": broccoli.Vector(dim=768),
        "price":     broccoli.Float(),
        "status":    broccoli.Keyword(),
    })
    idx.add({"id": "p1", "title": "Broccoli seeds", "embedding": vec,
             "price": 4.99, "status": "active"})

    hits = idx.search(text="organic seeds",
                      where={"status": "active", "price": broccoli.lt(10)},
                      k=10, explain=True)
    print(hits.plan)

The caller states intent; the optimizer decides which indexes to touch, how big
each candidate budget is, and how to rank. See document.md for the full design.
"""

from .engine import Index
from .eval import (Harness, Judgment, latency_at_fixed_recall, mrr, ndcg_at_k,
                   precision_at_k, recall_at_k, work_at_fixed_recall)
from .optimizer import Optimizer, Policy, QueryContext, RuleBasedPolicy
from .query import (Eq, Explain, Hit, OneOf, Plan, Predicate, Query, Range,
                    Results, between, gt, gte, lt, lte, one_of)
from .schema import Bool, Datetime, Float, Int, Keyword, Schema, Text, Vector
from .stats import StatisticsStore
from .types import Budget, Capabilities, CandidateSet, CostEstimate

__version__ = "0.1.0"

__all__ = [
    # engine
    "Index", "Schema",
    # schema fields
    "Text", "Vector", "Keyword", "Int", "Float", "Bool", "Datetime",
    # query + filters
    "Query", "Results", "Hit", "Explain", "Plan",
    "Predicate", "Eq", "OneOf", "Range",
    "lt", "lte", "gt", "gte", "between", "one_of",
    # optimizer
    "Optimizer", "Policy", "RuleBasedPolicy", "QueryContext",
    "Budget", "Capabilities", "CandidateSet", "CostEstimate", "StatisticsStore",
    # evaluation
    "Harness", "Judgment", "recall_at_k", "precision_at_k", "ndcg_at_k", "mrr",
    "latency_at_fixed_recall", "work_at_fixed_recall",
    "__version__",
]
