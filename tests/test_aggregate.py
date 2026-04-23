"""Unit tests for :mod:`harness.aggregate`.

Focus: the reviewer-survivable ratio math. Three properties we care about:

1. Geometric mean is the log-space mean (``exp(mean(log(xs)))``) and
   matches hand-computed values on a fixed fixture.
2. Workload-weighted mean collapses to the unweighted arithmetic mean
   when every weight is equal, and otherwise tilts toward the heavier
   samples.
3. ``summarize_ratios`` never includes non-positive / non-finite ratios
   in any reducer and records them in ``excluded`` with a reason string.
"""

from __future__ import annotations

import math

import pytest

from harness.aggregate import (
    LoopbackPolicy,
    WeightedSample,
    arithmetic_mean,
    geometric_mean,
    summarize_ratios,
    workload_weighted_mean,
)


# ---- gmean ---------------------------------------------------------------


def test_geometric_mean_matches_hand_computed_value():
    # sqrt(2 * 8) == 4
    assert geometric_mean([2.0, 8.0]) == pytest.approx(4.0)


def test_geometric_mean_log_space_equivalent_for_wide_spread():
    xs = [1.0, 10.0, 100.0, 1000.0]
    # hand-computed: (1 * 10 * 100 * 1000) ** 0.25 == 10**2.5 / 10**1.0 -> 10**(5/4)
    expected = 10.0 ** (sum(math.log10(x) for x in xs) / len(xs))
    assert geometric_mean(xs) == pytest.approx(expected)


def test_geometric_mean_handles_three_order_of_magnitude_spread():
    # Intentionally the spread the bench sees: ~150× to ~700× ratios.
    xs = [150.0, 300.0, 500.0, 700.0]
    got = geometric_mean(xs)
    # log-space mean should sit between 150 and 700, closer to the
    # geometric midpoint than the arithmetic one.
    assert 150.0 < got < 700.0
    assert got < arithmetic_mean(xs)  # gmean <= amean for positive reals


def test_geometric_mean_excludes_nonpositive():
    # log(0) is undefined; log(-5) is complex. The reducer silently
    # drops these (the audit trail in summarize_ratios records them).
    assert geometric_mean([2.0, 8.0, 0.0, -5.0]) == pytest.approx(4.0)


def test_geometric_mean_empty_returns_none():
    assert geometric_mean([]) is None
    assert geometric_mean([0.0, -1.0]) is None  # all excluded


# ---- workload-weighted mean ---------------------------------------------


def test_workload_weighted_mean_equals_arithmetic_when_equal_weights():
    samples = [
        WeightedSample(label="a", ratio=100.0, weight=1.0),
        WeightedSample(label="b", ratio=200.0, weight=1.0),
        WeightedSample(label="c", ratio=300.0, weight=1.0),
    ]
    assert workload_weighted_mean(samples) == pytest.approx(200.0)


def test_workload_weighted_mean_tilts_toward_heavy_sample():
    # Small topology (2 kRoutes) says 100x; large (500 kRoutes) says 300x.
    # Weighted mean should be close to 300, not 200.
    samples = [
        WeightedSample(label="toy", ratio=100.0, weight=2_000.0),
        WeightedSample(label="prod", ratio=300.0, weight=500_000.0),
    ]
    got = workload_weighted_mean(samples)
    assert got is not None
    assert 299.0 < got < 300.0  # tilted ~100% toward prod


def test_workload_weighted_mean_excludes_nonpositive_weights():
    samples = [
        WeightedSample(label="a", ratio=100.0, weight=10.0),
        WeightedSample(label="b", ratio=500.0, weight=0.0),  # excluded
        WeightedSample(label="c", ratio=500.0, weight=-1.0),  # excluded
    ]
    assert workload_weighted_mean(samples) == pytest.approx(100.0)


def test_workload_weighted_mean_all_invalid_returns_none():
    samples = [
        WeightedSample(label="a", ratio=0.0, weight=1.0),
        WeightedSample(label="b", ratio=100.0, weight=0.0),
    ]
    assert workload_weighted_mean(samples) is None


# ---- summarize_ratios ----------------------------------------------------


def test_summarize_ratios_emits_every_reducer_and_quantiles():
    samples = [
        WeightedSample(label=f"t{i}", ratio=float(r), weight=float(w))
        for i, (r, w) in enumerate([(100, 1), (200, 2), (300, 3), (400, 4)])
    ]
    s = summarize_ratios(samples, quantity="fair_ratio")
    assert s["quantity"] == "fair_ratio"
    assert s["n_total"] == 4
    assert s["n_used"] == 4
    assert s["excluded"] == []
    assert s["arithmetic_mean"] == pytest.approx(250.0)
    # gmean(100,200,300,400) = (2.4e9)**0.25 ~ 221.336
    assert s["geometric_mean"] == pytest.approx(221.336, rel=1e-3)
    # Workload-weighted: (1*100 + 2*200 + 3*300 + 4*400) / 10 = 300
    assert s["workload_weighted_mean"] == pytest.approx(300.0)
    assert s["min"] == 100.0
    assert s["max"] == 400.0
    # Linear-interpolation quantiles (type-7): on [100,200,300,400]
    # p25 = 100 + 0.75*(200-100) = 175; p50 = 250; p75 = 325.
    assert s["p25"] == pytest.approx(175.0)
    assert s["median"] == pytest.approx(250.0)
    assert s["p75"] == pytest.approx(325.0)
    assert len(s["samples"]) == 4


def test_summarize_ratios_records_exclusions_with_reason():
    import math as _m

    samples = [
        WeightedSample(label="ok", ratio=100.0, weight=1.0),
        WeightedSample(label="zero", ratio=0.0, weight=1.0),
        WeightedSample(label="neg", ratio=-1.0, weight=1.0),
        WeightedSample(label="nan", ratio=_m.nan, weight=1.0),
        WeightedSample(label="inf", ratio=_m.inf, weight=1.0),
    ]
    s = summarize_ratios(samples)
    assert s["n_total"] == 5
    assert s["n_used"] == 1
    labels = [e["label"] for e in s["excluded"]]
    assert set(labels) == {"zero", "neg", "nan", "inf"}
    for e in s["excluded"]:
        assert "reason" in e and e["reason"]
    # Exactly one sample survived — all reducers reflect that.
    assert s["arithmetic_mean"] == pytest.approx(100.0)
    assert s["geometric_mean"] == pytest.approx(100.0)
    assert s["workload_weighted_mean"] == pytest.approx(100.0)


def test_summarize_ratios_empty_input_returns_all_none():
    s = summarize_ratios([])
    assert s["n_total"] == 0
    assert s["n_used"] == 0
    assert s["excluded"] == []
    assert s["arithmetic_mean"] is None
    assert s["geometric_mean"] is None
    assert s["workload_weighted_mean"] is None
    assert s["median"] is None
    assert s["p25"] is None
    assert s["p75"] is None
    assert s["min"] is None
    assert s["max"] is None
    assert s["samples"] == []


def test_summarize_ratios_all_excluded_returns_all_none_keeps_audit():
    s = summarize_ratios(
        [WeightedSample(label="bad", ratio=0.0, weight=1.0)],
        quantity="wall_ratio",
    )
    assert s["quantity"] == "wall_ratio"
    assert s["n_total"] == 1
    assert s["n_used"] == 0
    assert len(s["excluded"]) == 1
    assert s["arithmetic_mean"] is None


# ---- LoopbackPolicy ------------------------------------------------------


def test_loopback_policy_from_bool_bridges_legacy_semantics():
    assert LoopbackPolicy.from_bool(True) is LoopbackPolicy.STRIP
    assert LoopbackPolicy.from_bool(False) is LoopbackPolicy.PASSTHROUGH


def test_loopback_policy_strip_flag_is_strip_only():
    assert LoopbackPolicy.STRIP.strip_loopback_host is True
    assert LoopbackPolicy.MATERIALIZE.strip_loopback_host is False
    assert LoopbackPolicy.PASSTHROUGH.strip_loopback_host is False


def test_loopback_policy_values_are_stable_strings():
    # Downstream CLI flags / reports key off these literals.
    assert LoopbackPolicy.STRIP.value == "strip"
    assert LoopbackPolicy.MATERIALIZE.value == "materialize"
    assert LoopbackPolicy.PASSTHROUGH.value == "passthrough"


# ---- arithmetic_mean -----------------------------------------------------


def test_arithmetic_mean_excludes_nonpositive():
    assert arithmetic_mean([10.0, 0.0, -1.0]) == pytest.approx(10.0)


def test_arithmetic_mean_empty_returns_none():
    assert arithmetic_mean([]) is None
