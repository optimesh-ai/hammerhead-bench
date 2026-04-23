"""Rigorous ratio aggregation — the reviewer-survivable math layer.

Reviewers rightfully object to headline speedups computed as the
arithmetic mean of per-topology ratios:

* A 1.0 s → 0.005 s topology (200×) and a 90.0 s → 0.5 s topology (180×)
  are not equally informative: the first is 2k routes, the second is
  540k routes. Averaging them flattens a 270× engineering datum behind
  a 200× toy datum.
* Ratios live on a multiplicative scale. The arithmetic mean of
  ``[2, 8]`` is 5 but the *typical* point is ``sqrt(2·8) = 4`` — the
  geometric mean. For log-scaled quantities (wall-clock ratios span
  three orders of magnitude in this corpus) gmean is the unbiased
  central-tendency estimator.
* Tiny topologies have high relative measurement noise. A 2-node rig
  that finishes in 20 ms has a ±10 ms jitter band — enough to swing
  the reported speedup by ±50%. Weighting by workload dampens that
  noise in the headline.

This module exports three reductions that always appear side-by-side
in every ``bench_summary.json`` consumer:

* :func:`arithmetic_mean` — the legacy naive mean, kept so old
  consumers don't break.
* :func:`geometric_mean` — the *typical* ratio; robust to
  multiplicative scale.
* :func:`workload_weighted_mean` — the arithmetic mean of ratios
  weighted by a workload scalar (route count, node count, total
  simulate time). Reviewers should cite this when the question is
  "what happens at production scale?".

:func:`summarize_ratios` rolls all three plus the range, p25/p50/p75,
and the per-sample trail into one dict. That dict is the canonical
payload surfaced in ``bench_summary.json`` under a top-level
``fair_ratio_summary`` key (and the matching ``wall_ratio_summary``
/``asym_ratio_summary``).

All reductions explicitly exclude ``None`` and non-positive ratios so
a failed topology (simulator crashed, ratio undefined) cannot skew
the aggregate. The exclusion is logged to the returned dict under
``excluded`` so reviewers can audit the denominator.

The companion :class:`LoopbackPolicy` enum lives here rather than in
``extract/fib.py`` so the aggregation test layer can import it
without pulling the canonicalization chain.
"""

from __future__ import annotations

import enum
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "LoopbackPolicy",
    "WeightedSample",
    "arithmetic_mean",
    "geometric_mean",
    "summarize_ratios",
    "workload_weighted_mean",
]


class LoopbackPolicy(str, enum.Enum):
    """Symmetric /32 loopback handling across all three FIB sources.

    FRR's zebra installs ``<loopback-ip>/32`` connected + local entries
    for every ``interface lo`` address. Batfish and Hammerhead both
    model the loopback as a prefix originator but do **not** re-install
    the /32 host entry in the FIB — they treat it as a source of routes,
    not a destination-of-itself. That asymmetry shows up as a 23 %
    presence gap on every IS-IS / OSPF rig in the corpus.

    The policy decides how the diff engine reconciles that gap:

    * :attr:`STRIP` — *reference canonicalizer, minimalist view.* Drop
      every ``lo*``-interface /32 ``connected``/``local`` entry from
      all three sources (vendor, Batfish, Hammerhead). The diff
      presence column reflects only routes the *control plane*
      originated. This is the default because it is the most honest
      apples-to-apples view: the two simulators are compared on the
      route set they actually modelled, and vendor truth is brought
      to the same surface rather than being given a free ride for
      routes it installs as a byproduct of interface-up.
    * :attr:`MATERIALIZE` — *completionist view.* Keep vendor /32
      host entries and synthesise the matching entries on the
      simulator side by traversing the loopback adjacency graph
      (every node's loopback becomes a /32 connected + local entry
      for that node). This is the strictest correctness check but
      requires the simulator to claim coverage over routes it did
      not solve for; Batfish and Hammerhead both refuse to emit them
      under the current APIs, so this mode is diagnostic-only —
      exposed so a reviewer can see the gap rather than arguing
      about it.
    * :attr:`PASSTHROUGH` — leave every source exactly as the
      adapter emitted it. Loopback /32 entries stay on vendor,
      stay off the simulators. Retained for pre-Wave-X consumers
      and debugging; the presence gap in this mode is the
      Batfish-favoring upper bound.
    """

    STRIP = "strip"
    MATERIALIZE = "materialize"
    PASSTHROUGH = "passthrough"

    @classmethod
    def from_bool(cls, filter_loopback_host: bool) -> "LoopbackPolicy":
        """Legacy boolean → enum bridge; preserves the pre-enum semantic."""
        return cls.STRIP if filter_loopback_host else cls.PASSTHROUGH

    @property
    def strip_loopback_host(self) -> bool:
        """True when the canonicalizer should drop /32 loopback host entries."""
        return self is LoopbackPolicy.STRIP


@dataclass(frozen=True, slots=True)
class WeightedSample:
    """One ratio observation with its workload weight.

    ``label`` is free-form (typically the topology name) and is only
    used for audit trails — it does not feed the math.
    ``ratio`` must be strictly positive; zero / negative / NaN
    observations are excluded from every reduction (and recorded in
    :func:`summarize_ratios`'s ``excluded`` list).
    ``weight`` must be strictly positive for the weighted mean to
    carry the sample. Zero-weight samples still contribute to the
    arithmetic and geometric means.
    """

    label: str
    ratio: float
    weight: float = 1.0


def arithmetic_mean(ratios: Iterable[float]) -> float | None:
    """Legacy ``sum(xs)/n`` over positive, finite ratios. ``None`` on empty input."""
    xs = [x for x in ratios if _is_positive_finite(x)]
    if not xs:
        return None
    return statistics.fmean(xs)


def geometric_mean(ratios: Iterable[float]) -> float | None:
    """``(prod xs)^(1/n)`` computed in log space for numerical stability.

    Why log space: ratios in this corpus span ~150× at k=4 to ~700×
    at k=20, and the product of 16 such numbers overflows float64 long
    before we reach the final root. ``exp(mean(log(xs)))`` is
    mathematically equivalent and numerically bulletproof.

    Returns ``None`` on empty input (no ratios to reduce). Zero and
    negative ratios are silently excluded because ``log(x<=0)`` is
    undefined; :func:`summarize_ratios` records the exclusions in
    its audit trail.
    """
    xs = [x for x in ratios if _is_positive_finite(x)]
    if not xs:
        return None
    return math.exp(statistics.fmean(math.log(x) for x in xs))


def workload_weighted_mean(samples: Iterable[WeightedSample]) -> float | None:
    """``sum(w_i * r_i) / sum(w_i)`` over positive weights + ratios.

    The natural weight for a benchmark corpus is the workload size
    (route count on the Batfish side, since that's the thing the
    solver actually computed). Weighting by workload collapses the
    headline to "what speedup does the operator see at production
    scale?" because production-scale topologies dominate production
    wall-clock.

    Returns ``None`` when no sample has both a positive ratio and a
    positive weight (all-zero-weight or all-invalid case).
    """
    num = 0.0
    denom = 0.0
    for s in samples:
        if not _is_positive_finite(s.ratio):
            continue
        if not _is_positive_finite(s.weight):
            continue
        num += s.weight * s.ratio
        denom += s.weight
    if denom <= 0.0:
        return None
    return num / denom


def summarize_ratios(
    samples: Sequence[WeightedSample],
    *,
    quantity: str = "ratio",
) -> dict:
    """Emit every reducer side-by-side so the reader picks.

    The returned dict has stable keys so bench_summary.json consumers
    can pattern-match. Shape::

        {
          "quantity": "fair_ratio",
          "n_total": 16,
          "n_used": 16,
          "excluded": [],
          "arithmetic_mean": 231.72,
          "geometric_mean": 218.44,
          "workload_weighted_mean": 274.10,
          "median": 223.1,
          "p25": 195.5,
          "p75": 268.3,
          "min": 168.2,
          "max": 362.4,
          "samples": [{"label": "...", "ratio": ..., "weight": ...}, ...]
        }

    ``quantity`` is a free-form label copied into the dict so the
    renderer can key off it ("fair_ratio_summary", "wall_ratio_summary",
    ...) without the caller re-stamping.

    Samples whose ``ratio`` is not positive-finite are moved to the
    ``excluded`` list with the reason verbatim; they do not feed any
    reducer. A zero-weight sample still feeds arithmetic / geometric
    means but cannot feed the weighted mean.
    """
    used: list[WeightedSample] = []
    excluded: list[dict] = []
    for s in samples:
        reason = _exclusion_reason(s)
        if reason is None:
            used.append(s)
        else:
            excluded.append({"label": s.label, "reason": reason, "ratio": s.ratio, "weight": s.weight})

    n_total = len(samples)
    n_used = len(used)

    if n_used == 0:
        return {
            "quantity": quantity,
            "n_total": n_total,
            "n_used": 0,
            "excluded": excluded,
            "arithmetic_mean": None,
            "geometric_mean": None,
            "workload_weighted_mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
            "samples": [],
        }

    ratios = [s.ratio for s in used]
    ratios_sorted = sorted(ratios)

    return {
        "quantity": quantity,
        "n_total": n_total,
        "n_used": n_used,
        "excluded": excluded,
        "arithmetic_mean": statistics.fmean(ratios),
        "geometric_mean": math.exp(statistics.fmean(math.log(r) for r in ratios)),
        "workload_weighted_mean": workload_weighted_mean(used),
        "median": _quantile(ratios_sorted, 0.5),
        "p25": _quantile(ratios_sorted, 0.25),
        "p75": _quantile(ratios_sorted, 0.75),
        "min": ratios_sorted[0],
        "max": ratios_sorted[-1],
        "samples": [
            {"label": s.label, "ratio": s.ratio, "weight": s.weight} for s in used
        ],
    }


# ---- internals -----------------------------------------------------------


def _is_positive_finite(x: float | None) -> bool:
    if x is None:
        return False
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(xf) and xf > 0.0


def _exclusion_reason(s: WeightedSample) -> str | None:
    if s.ratio is None:
        return "ratio is None"
    try:
        rf = float(s.ratio)
    except (TypeError, ValueError):
        return f"ratio {s.ratio!r} is not float-coercible"
    if math.isnan(rf):
        return "ratio is NaN"
    if math.isinf(rf):
        return "ratio is infinite"
    if rf <= 0.0:
        return f"ratio {rf} is not strictly positive"
    return None


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (type-7 in R's ``quantile`` idiom).

    ``sorted_xs`` must already be sorted ascending and non-empty.
    For n == 1 returns the single value. For n >= 2 computes
    ``sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * frac`` where
    ``idx = q * (n - 1)``. Matches ``numpy.quantile(method="linear")``
    on the same input without pulling numpy into the harness.
    """
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("quantile of empty sequence is undefined")
    if n == 1:
        return float(sorted_xs[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    frac = pos - lo
    if lo == hi:
        return float(sorted_xs[lo])
    return float(sorted_xs[lo]) + (float(sorted_xs[hi]) - float(sorted_xs[lo])) * frac
