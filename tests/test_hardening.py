"""Reviewer-hardening tests — the four objectives the performance-engineer
pass landed:

1. Persistent :class:`BatfishService` — the same container + pybatfish
   session feeds N ``run_one`` calls; the first reports ``warm=False`` +
   non-zero ``container_start_s``; every subsequent call reports
   ``warm=True`` + ``container_start_s == 0.0`` so the cold/warm split
   is preserved through the sidecar and the aggregate.
2. Reference Canonicalizer — the symmetric /32 loopback-host
   reconciler drops IGP-advertised host routes seen on only one of
   the two sim outputs, so IS-IS / OSPF topologies don't carry the
   historical 23 % presence gap. Under ``MATERIALIZE`` the same rows
   are mirrored onto the opposite side (completionist view).
3. Aggregate math — arithmetic mean, geometric mean,
   workload-weighted mean, and quantiles all appear side-by-side in
   the bench summary's ``fair_ratio_summary`` / ``wall_ratio_summary``
   / ``asym_ratio_summary`` blocks. A failed topology (ratio None or
   non-positive) lands in ``excluded`` rather than skewing the mean.
4. Peak RSS — ``BatfishStats`` carries ``peak_rss_mb`` / ``_source`` /
   ``_sample_count`` from the ``DockerStatsSampler`` and the pipeline
   rolls the per-trial mean into ``SimOnlyAgreement.batfish_peak_rss_mb``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.aggregate import (
    LoopbackPolicy,
    WeightedSample,
    arithmetic_mean,
    geometric_mean,
    summarize_ratios,
    workload_weighted_mean,
)
from harness.diff.engine import _RouteKey
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import (
    BenchHooks,
    SimOnlyAgreement,
    _looks_like_loopback_host,
    _reconcile_loopback_host_routes,
    aggregate_sim_only,
    run_topology_sim_only,
)
from harness.tools.batfish import (
    BatfishConfig,
    BatfishService,
    BatfishStats,
)
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"


# ---- Objective 1: persistent BatfishService ------------------------------


class _StubSession:
    """In-memory BatfishSession. Tracks init_snapshot call count so a test
    can assert the service re-used its pybatfish session rather than
    spinning up a fresh one per call."""

    def __init__(
        self,
        *,
        route_rows: list[dict[str, Any]] | None = None,
        bgp_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.init_snapshot_calls = 0
        self.get_routes_calls = 0
        self.get_bgp_rib_calls = 0
        self._routes = route_rows or []
        self._bgp = bgp_rows or []

    def init_snapshot(self, path: str, name: str, overwrite: bool = True) -> str:
        self.init_snapshot_calls += 1
        return name

    def get_routes(self) -> list[dict[str, Any]]:
        self.get_routes_calls += 1
        return list(self._routes)

    def get_bgp_rib(self) -> list[dict[str, Any]]:
        self.get_bgp_rib_calls += 1
        return list(self._bgp)


class _StubRunner:
    def __init__(self) -> None:
        self.started = 0
        self.stopped: list[str] = []

    def start(self, cfg: BatfishConfig) -> str:
        self.started += 1
        return f"stub-container-{self.started}"

    def wait_ready(self, cfg: BatfishConfig, container_id: str) -> None:
        # Simulate readiness latency so container_start_s is non-zero.
        time.sleep(0.005)

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


def _connected_loopback_rows(nodes: Sequence[str]) -> list[dict[str, Any]]:
    """Fixture row set for N nodes with one /32 loopback each."""
    return [
        {
            "Node": n,
            "VRF": "default",
            "Network": f"10.0.0.{i + 1}/32",
            "Protocol": "connected",
            "Next_Hop": {"interface": "lo"},
        }
        for i, n in enumerate(nodes)
    ]


def test_batfish_service_start_once_warm_true_after_first(tmp_path: Path) -> None:
    """First run_one reports warm=False + container_start_s>0; every
    subsequent run_one on the same service reports warm=True +
    container_start_s=0. The underlying container is started exactly
    once and stopped exactly once across the lifecycle."""
    runner = _StubRunner()
    session = _StubSession(route_rows=_connected_loopback_rows(["r1", "r2"]))
    svc = BatfishService(
        runner=runner,
        session_factory=lambda _cfg: session,
        sample_memory=False,
    )

    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_c = tmp_path / "c"

    stats_a = svc.run_one(cfgs, out_a, topology="t_a")
    stats_b = svc.run_one(cfgs, out_b, topology="t_b")
    stats_c = svc.run_one(cfgs, out_c, topology="t_c")

    assert runner.started == 1
    assert runner.stopped == []  # not closed yet

    # First call pays the cold-start cost; subsequent calls are warm.
    assert stats_a.warm is False
    assert stats_a.container_start_s > 0.0
    assert stats_b.warm is True
    assert stats_b.container_start_s == 0.0
    assert stats_c.warm is True
    assert stats_c.container_start_s == 0.0

    # Session is re-used: init_snapshot fires once per call, not once
    # per container spin-up. Three calls = three snapshots.
    assert session.init_snapshot_calls == 3
    assert session.get_routes_calls == 3
    assert session.get_bgp_rib_calls == 3

    svc.close()
    assert runner.stopped == ["stub-container-1"]


def test_batfish_service_context_manager_closes_on_exit(tmp_path: Path) -> None:
    """Using BatfishService as a context manager starts once and stops
    once, even when the inner block raises."""
    runner = _StubRunner()
    session = _StubSession()
    cfgs = tmp_path / "configs"
    cfgs.mkdir()

    with pytest.raises(RuntimeError, match="inner body"), BatfishService(
        runner=runner,
        session_factory=lambda _cfg: session,
        sample_memory=False,
    ):
        raise RuntimeError("inner body")

    assert runner.started == 1
    assert runner.stopped == ["stub-container-1"]


def test_batfish_service_start_failure_tears_down_container(tmp_path: Path) -> None:
    """If wait_ready raises, the half-spawned container must still be
    stopped so we don't leak a JVM."""

    class _FailingRunner(_StubRunner):
        def wait_ready(self, cfg: BatfishConfig, container_id: str) -> None:
            raise TimeoutError("simulated readiness timeout")

    runner = _FailingRunner()
    svc = BatfishService(
        runner=runner,
        session_factory=lambda _cfg: _StubSession(),
        sample_memory=False,
    )
    with pytest.raises(TimeoutError, match="readiness"):
        svc.start()
    assert runner.stopped == ["stub-container-1"]
    assert svc.started is False  # reset so double-close is idempotent
    svc.close()  # no-op; must not raise


def test_run_topology_sim_only_uses_service_warm_vs_cold(tmp_path: Path) -> None:
    """End-to-end: plumbing a persistent service through BenchHooks
    produces warm-vs-cold split in the resulting SimOnlyAgreement."""
    spec = load_spec(TOPO_DIR)
    runner = _StubRunner()
    session = _StubSession(
        route_rows=_connected_loopback_rows(["r1", "r2"]),
    )
    service = BatfishService(
        runner=runner,
        session_factory=lambda _cfg: session,
        sample_memory=False,
    )
    service.start()

    # Hammerhead side: canned hook that writes an identical FIB pair and
    # a sidecar so the trial loop has real sidecar timings to read.
    def _hh_hook(configs_dir: Path, out_dir: Path, topology: str) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, node in enumerate(("r1", "r2"), start=1):
            fib = NodeFib(
                node=node,
                vrf="default",
                source="hammerhead",
                routes=[
                    Route(
                        prefix=f"10.0.0.{i}/32",
                        protocol="connected",
                        next_hops=[NextHop(interface="lo")],
                    )
                ],
            )
            (out_dir / f"{node}__default.json").write_text(fib.model_dump_json())
        (out_dir / "hammerhead_stats.json").write_text(
            json.dumps(
                {
                    "simulate_s": 0.01,
                    "rib_total_s": 0.0,
                    "total_s": 0.02,
                    "peak_rss_mb": 42,
                }
            )
        )

    hooks = BenchHooks(
        batfish_service=service,
        hammerhead=_hh_hook,
    )

    try:
        result = run_topology_sim_only(
            spec,
            workdir=tmp_path / "workdir",
            results_dir=tmp_path / "results",
            hooks=hooks,
            trials=3,
        )
    finally:
        service.close()

    assert result.status == "passed", result.error
    a = result.agreement
    assert a is not None

    # Trial payload has per-trial data: first trial cold, rest warm.
    assert a.trials is not None
    assert a.trials["n"] == 3
    # Our stub's per-call simulate_s is ~0 (no sleep inside query), so
    # warm_mean and cold are both tiny; the structural guarantees are
    # what we assert — not numeric thresholds.
    assert a.batfish_simulate_s_cold is not None
    assert a.batfish_simulate_s_warm_mean is not None  # 2 warm trials
    assert a.batfish_container_start_s is not None
    assert a.batfish_container_start_s >= 0.005  # stub wait_ready sleeps

    # Hammerhead peak RSS flowed through the sidecar into the agreement.
    assert a.hammerhead_peak_rss_mb == pytest.approx(42.0)


# ---- Objective 2: Reference Canonicalizer --------------------------------


def _route(protocol: str, prefix: str, iface: str | None = "lo") -> Route:
    return Route(
        prefix=prefix,
        protocol=protocol,  # type: ignore[arg-type]
        next_hops=[NextHop(ip=None, interface=iface)] if iface else [],
    )


def test_looks_like_loopback_host_matches_connected_lo_32() -> None:
    assert _looks_like_loopback_host(_route("connected", "10.0.0.1/32", iface="lo")) is True
    assert _looks_like_loopback_host(_route("connected", "10.0.0.1/32", iface="Loopback0")) is True
    # Non-loopback interface /32: not a loopback host.
    assert _looks_like_loopback_host(_route("connected", "10.0.0.1/32", iface="eth0")) is False


def test_looks_like_loopback_host_matches_isis_ospf_32() -> None:
    # IGP-advertised /32 (IS-IS / OSPF) qualifies regardless of interface name.
    assert _looks_like_loopback_host(_route("isis", "10.0.0.5/32", iface="eth1")) is True
    assert _looks_like_loopback_host(_route("ospf", "10.0.0.5/32", iface=None)) is True


def test_looks_like_loopback_host_rejects_bgp_and_static() -> None:
    # A /32 coming from BGP or static is user-configured, not adapter asymmetry.
    assert _looks_like_loopback_host(_route("bgp", "10.0.0.5/32", iface="eth1")) is False
    assert _looks_like_loopback_host(_route("static", "10.0.0.5/32", iface="eth1")) is False


def test_looks_like_loopback_host_rejects_non_32() -> None:
    assert _looks_like_loopback_host(_route("isis", "10.0.0.0/24", iface=None)) is False


def _ix(rows: list[tuple[str, str, str, Route]]) -> dict:
    return {_RouteKey(node=n, vrf=v, prefix=p): r for n, v, p, r in rows}


def test_reconcile_strip_removes_asymmetric_igp_loopback() -> None:
    bf = _ix([("r1", "default", "10.0.0.2/32", _route("isis", "10.0.0.2/32", iface=None))])
    hh: dict = _ix([])
    bf_out, hh_out, reconciled = _reconcile_loopback_host_routes(
        bf, hh, policy=LoopbackPolicy.STRIP
    )
    # Symmetric strip: the asymmetric row drops from BOTH sides.
    assert bf_out == {}
    assert hh_out == {}
    assert reconciled == 1


def test_reconcile_passthrough_is_noop() -> None:
    bf = _ix([("r1", "default", "10.0.0.2/32", _route("isis", "10.0.0.2/32"))])
    hh = _ix([("r1", "default", "10.0.0.3/24", _route("bgp", "10.0.0.3/24", iface="eth1"))])
    bf_out, hh_out, reconciled = _reconcile_loopback_host_routes(
        bf, hh, policy=LoopbackPolicy.PASSTHROUGH
    )
    assert bf_out == bf
    assert hh_out == hh
    assert reconciled == 0


def test_reconcile_materialize_mirrors_missing_side() -> None:
    bf_route = _route("ospf", "10.0.0.9/32", iface=None)
    bf = _ix([("r1", "default", "10.0.0.9/32", bf_route)])
    hh: dict = _ix([])
    bf_out, hh_out, reconciled = _reconcile_loopback_host_routes(
        bf, hh, policy=LoopbackPolicy.MATERIALIZE
    )
    # Phantom copy inserted on HH side so the row enters B ∩ H.
    key = _RouteKey(node="r1", vrf="default", prefix="10.0.0.9/32")
    assert key in bf_out and key in hh_out
    assert hh_out[key] is bf_route  # mirrored, not freshly built
    assert reconciled == 1


def test_reconcile_keeps_both_sided_rows_untouched() -> None:
    r = _route("isis", "10.0.0.1/32", iface=None)
    bf = _ix([("r1", "default", "10.0.0.1/32", r)])
    hh = _ix([("r1", "default", "10.0.0.1/32", r)])
    bf_out, hh_out, reconciled = _reconcile_loopback_host_routes(
        bf, hh, policy=LoopbackPolicy.STRIP
    )
    assert bf_out == bf
    assert hh_out == hh
    assert reconciled == 0


# ---- Objective 3: aggregate math -----------------------------------------


def test_arithmetic_geometric_workload_differ() -> None:
    """Sanity: the three reductions give numerically distinct answers
    on a corpus with heterogeneous ratios + weights. If they ever
    collapse to the same value, one of the reducers has regressed."""
    ratios = [2.0, 8.0, 200.0]
    weights = [1.0, 1.0, 5.0]
    assert arithmetic_mean(ratios) == pytest.approx(70.0)
    # (2*8*200)^(1/3) = 3200^(1/3) ≈ 14.736
    assert geometric_mean(ratios) == pytest.approx(3200 ** (1 / 3))
    # Workload pulls the headline toward the heaviest sample.
    samples = [WeightedSample("a", r, w) for r, w in zip(ratios, weights, strict=True)]
    wm = workload_weighted_mean(samples)
    # = (2 + 8 + 200*5) / (1+1+5) = 1010/7 ≈ 144.29
    assert wm == pytest.approx(1010 / 7)


def test_summarize_ratios_excludes_invalid_without_polluting_mean() -> None:
    samples = [
        WeightedSample("a", 200.0, 1000.0),
        WeightedSample("b", 300.0, 500.0),
        WeightedSample("c", None, 100.0),  # type: ignore[arg-type] — simulating bad data
        WeightedSample("d", 0.0, 100.0),
        WeightedSample("e", -5.0, 100.0),
    ]
    summary = summarize_ratios(samples, quantity="fair_ratio")
    assert summary["n_total"] == 5
    assert summary["n_used"] == 2
    assert len(summary["excluded"]) == 3
    reasons = {ex["reason"] for ex in summary["excluded"]}
    assert "ratio is None" in reasons
    assert any("not strictly positive" in r for r in reasons)
    assert summary["arithmetic_mean"] == pytest.approx(250.0)
    assert summary["geometric_mean"] == pytest.approx((200 * 300) ** 0.5)
    assert summary["workload_weighted_mean"] == pytest.approx(
        (1000 * 200 + 500 * 300) / 1500
    )


def test_aggregate_sim_only_emits_ratio_summaries_with_workload_weights() -> None:
    """``aggregate_sim_only`` produces a ``fair_ratio_summary`` etc.
    block whose workload-weighted mean picks up the Batfish route
    count as the weight. Reviewers should be able to read a single
    dict and see all three reductions side-by-side."""

    def _agree(topology: str, bf_routes: int, fair: float, wall: float) -> SimOnlyAgreement:
        # Back into a sim-only agreement that will produce the given
        # solve_plus_materialize_ratio / wall_ratio.
        return SimOnlyAgreement(
            topology=topology,
            batfish_routes=bf_routes,
            hammerhead_routes=bf_routes,
            union_keys=bf_routes,
            both_sides_keys=bf_routes,
            next_hop_agreement=1.0,
            protocol_agreement=1.0,
            bgp_attr_agreement=1.0,
            batfish_wall_s=wall,
            hammerhead_wall_s=1.0,
            batfish_simulate_s=fair,
            hammerhead_simulate_s=1.0,
            hammerhead_rib_total_s=0.0,
            hammerhead_simulate_plus_rib_s=1.0,
        )

    per = [
        _agree("tiny", bf_routes=4, fair=100.0, wall=50.0),
        _agree("large", bf_routes=10_000, fair=200.0, wall=300.0),
    ]
    summary = aggregate_sim_only(per)

    # Arithmetic means (naive).
    assert summary["fair_ratio_summary"]["arithmetic_mean"] == pytest.approx(150.0)
    # Workload-weighted mean heavily favours the large topology.
    wm = summary["fair_ratio_summary"]["workload_weighted_mean"]
    assert wm == pytest.approx(
        (4 * 100.0 + 10_000 * 200.0) / (4 + 10_000)
    )
    # Geometric mean is the square root of 100*200 = sqrt(20000) ≈ 141.42.
    assert summary["fair_ratio_summary"]["geometric_mean"] == pytest.approx(
        (100 * 200) ** 0.5
    )
    # wall_ratio summary also rendered.
    assert "wall_ratio_summary" in summary
    assert summary["wall_ratio_summary"]["n_used"] == 2


# ---- Objective 4: peak RSS flows through the sidecar --------------------


def test_batfish_stats_carries_peak_rss_fields() -> None:
    """BatfishStats has all three peak_rss columns; the as_dict round-
    trips them so a downstream consumer reading the sidecar JSON can
    key off peak_rss_mb directly."""
    s = BatfishStats(
        topology="fixture",
        started_iso="2026-04-22T00:00:00+0000",
        init_snapshot_s=0.1,
        query_routes_s=0.2,
        query_bgp_s=0.3,
        simulate_s=0.5,
        total_s=1.0,
        warm=True,
        container_start_s=0.0,
        peak_rss_mb=1500,
        peak_rss_source="docker-stats",
        peak_rss_sample_count=12,
    )
    d = s.as_dict()
    assert d["peak_rss_mb"] == 1500
    assert d["peak_rss_source"] == "docker-stats"
    assert d["peak_rss_sample_count"] == 12
    assert d["warm"] is True
    assert d["container_start_s"] == 0.0
