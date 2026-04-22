"""Tests for ``run_topology_sim_only`` + the ``--trials N`` CLI flag.

Covers:

- Default ``trials=1`` keeps the scalar shape and leaves
  ``agreement.trials`` / ``agreement.trial_stats`` as ``None`` (byte-for-byte
  compatible with pre-trials consumers).
- ``trials=3`` fires each hook N times, records per-trial wall-clocks, and
  produces ``mean/std/min/max`` summaries whose means equal the list
  arithmetic mean.
- ``trials=0`` is rejected with ``ValueError``.
- The CLI ``bench`` subcommand rejects ``--trials 5`` without ``--sim-only``
  (3-way truth path has no mileage for timing variance).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from harness.cli import main
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import BenchHooks, run_topology_sim_only
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"


def _nodefib(node: str, source: str) -> NodeFib:
    """One-route loopback /32 FIB for a given node + source stamp."""
    lo = "10.0.0.1" if node == "r1" else "10.0.0.2"
    return NodeFib(
        node=node,
        vrf="default",
        source=source,
        routes=[
            Route(
                prefix=f"{lo}/32",
                protocol="connected",
                next_hops=[NextHop(interface="lo")],
                admin_distance=0,
                metric=0,
            ),
        ],
    )


@dataclass
class _CountingHook:
    """Callable hook that stamps out an identical FIB each trial and tracks call count.

    Also writes a ``<source>_stats.json`` sidecar so the pipeline's
    ``_read_stat`` path records a simulate-time reading per trial.
    """

    source: str  # "batfish" | "hammerhead"
    simulate_s: float = 0.42
    rib_total_s: float = 0.0
    calls: int = field(default=0)

    def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        for node in ("r1", "r2"):
            fib = _nodefib(node, self.source)
            (out_dir / f"{node}__{fib.vrf}.json").write_text(fib.model_dump_json())
        sidecar = out_dir / f"{self.source}_stats.json"
        # Stamp simulate_s (the inner-solver field the pipeline now reads)
        # equal to the test's configured scalar and total_s just slightly
        # larger so the pipeline's simulate_s <= total_s guardrail is
        # satisfied without making the mean math noisy. Hammerhead stats
        # also carry ``rib_total_s`` — the per-device RIB materialisation
        # cost that pairs with Batfish's ``query_routes + query_bgp`` in
        # the fair solve+materialize ratio.
        payload: dict[str, float] = {
            "simulate_s": self.simulate_s,
            "total_s": self.simulate_s + self.rib_total_s + 0.001,
            "init_snapshot_s": 0.0,
        }
        if self.source == "hammerhead":
            payload["rib_total_s"] = self.rib_total_s
        sidecar.write_text(json.dumps(payload))


def test_run_topology_sim_only_default_trials_has_no_trial_payload(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CountingHook(source="batfish"),
        hammerhead=_CountingHook(source="hammerhead"),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
    )
    assert result.status == "passed", result.error
    a = result.agreement
    assert a is not None
    # Scalar fields populated (single-trial = mean-of-one).
    assert a.batfish_wall_s is not None
    assert a.hammerhead_wall_s is not None
    # Trials + trial_stats payloads only materialise at n >= 2.
    assert a.trials is None
    assert a.trial_stats is None


def test_run_topology_sim_only_trials_collects_per_trial_timings(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    bf_hook = _CountingHook(source="batfish", simulate_s=0.77)
    hh_hook = _CountingHook(source="hammerhead", simulate_s=0.11)
    hooks = BenchHooks(batfish=bf_hook, hammerhead=hh_hook)
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
        trials=3,
    )
    assert result.status == "passed", result.error
    # Each hook fired exactly 3 times — rendering runs once, only the
    # simulator loop repeats.
    assert bf_hook.calls == 3
    assert hh_hook.calls == 3

    a = result.agreement
    assert a is not None
    assert a.trials is not None
    assert a.trials["n"] == 3
    assert len(a.trials["batfish_wall_s"]) == 3
    assert len(a.trials["hammerhead_wall_s"]) == 3
    # simulate_s is read from the sidecar each trial → 3 readings each.
    assert a.trials["batfish_simulate_s"] == pytest.approx([0.77, 0.77, 0.77])
    assert a.trials["hammerhead_simulate_s"] == pytest.approx([0.11, 0.11, 0.11])

    # Scalar mean fields equal the arithmetic mean of the per-trial list.
    assert a.batfish_wall_s == pytest.approx(statistics.fmean(a.trials["batfish_wall_s"]))
    assert a.hammerhead_wall_s == pytest.approx(statistics.fmean(a.trials["hammerhead_wall_s"]))
    assert a.batfish_simulate_s == pytest.approx(0.77)
    assert a.hammerhead_simulate_s == pytest.approx(0.11)

    # trial_stats carries {mean, std, min, max} for each timing series.
    stats = a.trial_stats
    assert stats is not None
    for field_name in ("batfish_wall_s", "hammerhead_wall_s", "batfish_simulate_s", "hammerhead_simulate_s"):
        assert field_name in stats
        payload = stats[field_name]
        assert set(payload) == {"mean", "std", "min", "max"}
        assert payload["min"] <= payload["mean"] <= payload["max"]
    # Identical simulate_s readings → std == 0.
    assert stats["batfish_simulate_s"]["std"] == pytest.approx(0.0)
    assert stats["hammerhead_simulate_s"]["std"] == pytest.approx(0.0)


def test_agreement_exposes_presence_and_solve_ratio(tmp_path: Path) -> None:
    """Agreement JSON carries ``presence`` (Jaccard alias of ``coverage``)
    and ``solve_ratio`` (``batfish_simulate_s / hammerhead_simulate_s``).
    Both are needed by README §1 table + §2 formal definition."""
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CountingHook(source="batfish", simulate_s=0.84),
        hammerhead=_CountingHook(source="hammerhead", simulate_s=0.02),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
    )
    a = result.agreement
    assert a is not None
    d = a.as_dict()
    # presence == coverage (Jaccard), both exposed.
    assert "presence" in d
    assert "coverage" in d
    assert d["presence"] == d["coverage"] == a.coverage
    # Both-present case on the tiny fixture → identical FIBs → Jaccard 1.0.
    assert d["presence"] == pytest.approx(1.0)
    # solve_ratio uses the simulate_s sidecar (not wall).
    assert d["solve_ratio"] == pytest.approx(0.84 / 0.02)
    assert a.solve_ratio() == pytest.approx(0.84 / 0.02)


def test_agreement_exposes_fair_solve_plus_materialize_ratio(tmp_path: Path) -> None:
    """Headline ratio is ``bf_simulate_s / (hh_simulate_s + hh_rib_total_s)``.
    Pairs Batfish's fused query+materialize against Hammerhead's
    simulate+rib subprocesses — the only apples-to-apples comparison."""
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CountingHook(source="batfish", simulate_s=0.90),
        # HH: 0.02s in the simulator, 0.18s in the rib subprocess →
        # fair denominator 0.20, asymmetric denominator 0.02.
        hammerhead=_CountingHook(source="hammerhead", simulate_s=0.02, rib_total_s=0.18),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
    )
    a = result.agreement
    assert a is not None
    # Raw per-side fields.
    assert a.hammerhead_rib_total_s == pytest.approx(0.18)
    assert a.hammerhead_simulate_plus_rib_s == pytest.approx(0.20)
    # Fair ratio is the headline; asymmetric ratio is retained as a lower
    # bound. Fair < asymmetric because rib inflates HH's denominator.
    assert a.solve_plus_materialize_ratio() == pytest.approx(0.90 / 0.20)  # 4.5x
    assert a.solve_ratio() == pytest.approx(0.90 / 0.02)  # 45x (misleading)
    d = a.as_dict()
    assert d["solve_plus_materialize_ratio"] == pytest.approx(0.90 / 0.20)
    assert d["solve_ratio"] == pytest.approx(0.90 / 0.02)
    assert "hammerhead_rib_total_s" in d
    assert "hammerhead_simulate_plus_rib_s" in d


def test_solve_plus_materialize_ratio_returns_none_without_rib_stat() -> None:
    """When the Hammerhead sidecar predates the rib_total_s field, the fair
    ratio is None — never synthesise a fake denominator."""
    from harness.pipeline import SimOnlyAgreement  # noqa: PLC0415

    a = SimOnlyAgreement(
        topology="legacy",
        batfish_routes=4,
        hammerhead_routes=4,
        union_keys=4,
        both_sides_keys=4,
        next_hop_agreement=1.0,
        protocol_agreement=1.0,
        bgp_attr_agreement=1.0,
        batfish_simulate_s=5.0,
        hammerhead_simulate_s=0.05,
        hammerhead_rib_total_s=None,
        hammerhead_simulate_plus_rib_s=None,
    )
    assert a.solve_plus_materialize_ratio() is None
    # Asymmetric ratio still renders so legacy sidecars aren't silently dropped.
    assert a.solve_ratio() == pytest.approx(5.0 / 0.05)


def test_solve_ratio_returns_none_when_simulate_stat_missing() -> None:
    from harness.pipeline import SimOnlyAgreement  # noqa: PLC0415

    a = SimOnlyAgreement(
        topology="toy",
        batfish_routes=4,
        hammerhead_routes=4,
        union_keys=4,
        both_sides_keys=4,
        next_hop_agreement=1.0,
        protocol_agreement=1.0,
        bgp_attr_agreement=1.0,
        batfish_simulate_s=None,
        hammerhead_simulate_s=0.5,
    )
    assert a.solve_ratio() is None
    assert a.as_dict()["solve_ratio"] is None

    b = SimOnlyAgreement(
        topology="toy",
        batfish_routes=4,
        hammerhead_routes=4,
        union_keys=4,
        both_sides_keys=4,
        next_hop_agreement=1.0,
        protocol_agreement=1.0,
        bgp_attr_agreement=1.0,
        batfish_simulate_s=10.0,
        hammerhead_simulate_s=0.0,
    )
    assert b.solve_ratio() is None


def test_run_topology_sim_only_rejects_trials_zero(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    with pytest.raises(ValueError, match="trials must be >= 1"):
        run_topology_sim_only(
            spec,
            workdir=tmp_path / "workdir",
            results_dir=tmp_path / "results",
            trials=0,
        )


@dataclass
class _SimulateExceedsTotalHook:
    """Hook that stamps a sidecar where ``simulate_s > total_s + tolerance``.

    Proves the pipeline's ``_assert_simulate_le_total`` guardrail fires
    loudly at benchmark-time — the regression it's designed to catch is
    a re-aliasing of ``simulate_s`` to any wall-clock stat.
    """

    source: str

    def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
        out_dir.mkdir(parents=True, exist_ok=True)
        for node in ("r1", "r2"):
            lo = "10.0.0.1" if node == "r1" else "10.0.0.2"
            fib = NodeFib(
                node=node,
                vrf="default",
                source=self.source,
                routes=[
                    Route(
                        prefix=f"{lo}/32",
                        protocol="connected",
                        next_hops=[NextHop(interface="lo")],
                        admin_distance=0,
                        metric=0,
                    ),
                ],
            )
            (out_dir / f"{node}__{fib.vrf}.json").write_text(fib.model_dump_json())
        (out_dir / f"{self.source}_stats.json").write_text(
            # 10s simulate vs 1s total — classic re-aliasing regression shape.
            json.dumps({"simulate_s": 10.0, "total_s": 1.0, "init_snapshot_s": 0.0})
        )


def test_pipeline_rejects_simulate_greater_than_total(tmp_path: Path) -> None:
    """Guardrail: catch the 2026-04-22 regression where ``simulate_s`` was
    aliased to ``total_s``. If a wrapper ever stamps a sidecar whose
    ``simulate_s`` exceeds ``total_s`` by > 100 ms, the pipeline must
    fail the topology rather than silently accept inflated solve ratios."""
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_SimulateExceedsTotalHook(source="batfish"),
        hammerhead=_SimulateExceedsTotalHook(source="hammerhead"),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
    )
    assert result.status == "failed"
    assert result.error is not None
    assert "simulate_s" in result.error
    assert "total_s" in result.error


def test_agreement_surfaces_init_snapshot_s(tmp_path: Path) -> None:
    """``batfish_init_snapshot_s`` is the Batfish-only snapshot-upload cost.
    Must land on :class:`SimOnlyAgreement` + in the JSON payload so the
    report layer can separate "snapshot upload" from "real solve"."""
    spec = load_spec(TOPO_DIR)

    @dataclass
    class _InitStampedHook:
        source: str
        simulate_s: float = 0.5
        init_s: float = 7.3

        def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
            out_dir.mkdir(parents=True, exist_ok=True)
            for node in ("r1", "r2"):
                lo = "10.0.0.1" if node == "r1" else "10.0.0.2"
                fib = NodeFib(
                    node=node,
                    vrf="default",
                    source=self.source,
                    routes=[
                        Route(
                            prefix=f"{lo}/32",
                            protocol="connected",
                            next_hops=[NextHop(interface="lo")],
                            admin_distance=0,
                            metric=0,
                        ),
                    ],
                )
                (out_dir / f"{node}__{fib.vrf}.json").write_text(fib.model_dump_json())
            (out_dir / f"{self.source}_stats.json").write_text(
                json.dumps(
                    {
                        "simulate_s": self.simulate_s,
                        "total_s": self.simulate_s + self.init_s + 0.5,
                        "init_snapshot_s": self.init_s,
                    }
                )
            )

    hooks = BenchHooks(
        batfish=_InitStampedHook(source="batfish", simulate_s=0.5, init_s=7.3),
        # Hammerhead has no init_snapshot concept; the field stays None.
        hammerhead=_InitStampedHook(source="hammerhead", simulate_s=0.05, init_s=0.0),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
    )
    assert result.status == "passed", result.error
    a = result.agreement
    assert a is not None
    assert a.batfish_init_snapshot_s == pytest.approx(7.3)
    assert "batfish_init_snapshot_s" in a.as_dict()


def test_bench_cli_rejects_trials_without_sim_only(tmp_path: Path) -> None:
    """``--trials N > 1`` is only honoured under ``--sim-only``. The CLI
    rejects the mismatch with a non-zero exit + a clear stderr message."""
    runner = CliRunner()
    # We don't need an actual topology: the validator runs before selection.
    result = runner.invoke(
        main,
        ["bench", "--trials", "5", "--only", "bgp-ibgp-2node"],
        # no --sim-only → should fail validation.
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "--trials" in combined
    assert "sim-only" in combined
