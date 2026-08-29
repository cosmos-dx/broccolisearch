"""Robust linear calibration shared by every index engine.

Why this exists: a cost model of the form `latency = work x slope` is wrong for
small queries. Every operator has a fixed per-call cost (function dispatch,
allocation, sorting) that dominates when the work is small, so calibrating only
a slope makes the optimizer confidently underestimate cheap plans — and a
confidently wrong optimizer picks bad plans (Approach.md §7).

So we fit `latency = base + work x slope` from real measurements taken on the
machine and corpus we are actually running on.

The fit is Theil-Sen (median of pairwise slopes), not ordinary least squares.
OLS assumes symmetric noise; timing noise is one-sided and heavy-tailed — a
process can only be interrupted and made slower, never made faster than the
hardware allows. A single descheduled sample drags an OLS slope a long way.
Measured across repeated calibrations of the same corpus, OLS produced
constants that varied by up to four orders of magnitude run to run, so the
optimizer's plan choice moved for reasons that had nothing to do with the
query. Theil-Sen tolerates ~29% outliers and made those constants repeatable.
"""

from __future__ import annotations

from statistics import median
from typing import Sequence, Tuple

Point = Tuple[float, float]  # (work_units, seconds)

TIMING_REPEATS = 9
"""Timings per calibration point, combined with min().

The fastest run is the least contaminated by scheduler noise, so it estimates
true cost better than a mean. Three repeats left enough noise in each point to
destabilise the fit; nine is still only a few seconds of calibration.
"""


def fit_linear(points: Sequence[Point], min_slope: float = 1e-10,
               ) -> Tuple[float, float]:
    """Robust line through (work, seconds) → (base_s, s_per_unit).

    Both outputs are clamped non-negative: a negative fixed cost or a negative
    marginal cost is never physically meaningful, and letting one through would
    make the optimizer prefer plans that "cost less the more they do".
    """
    pts = [(float(w), float(t)) for w, t in points if w >= 0 and t >= 0]
    if not pts:
        return 0.0, min_slope
    if len(pts) == 1:
        work, seconds = pts[0]
        if work <= 0:
            return seconds, min_slope
        return 0.0, max(seconds / work, min_slope)

    n = len(pts)
    slopes = [(pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0])
              for i in range(n) for j in range(i + 1, n)
              if pts[j][0] != pts[i][0]]

    if not slopes:  # all samples at the same work level
        mean_work = sum(w for w, _ in pts) / n
        mean_time = median([t for _, t in pts])
        if mean_work <= 0:
            return mean_time, min_slope
        return 0.0, max(mean_time / mean_work, min_slope)

    slope = median(slopes)
    base = median([t - slope * w for w, t in pts])
    return max(base, 0.0), max(slope, min_slope)
