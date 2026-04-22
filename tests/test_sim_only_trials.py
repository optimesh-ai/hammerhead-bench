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
    calls: int = field(default=0)

    def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        for node in ("r1", "r2"):
            fib = _nodefib(node, self.source)
            (out_dir / f"{node}__{fib.vrf}.json").write_text(fib.model_dump_json())
        sidecar = out_dir / f"{self.source}_stats.json"
        sidecar.write_text(json.dumps({"total_s": self.simulate_s}))


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


def test_run_topology_sim_only_rejects_trials_zero(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    with pytest.raises(ValueError, match="trials must be >= 1"):
        run_topology_sim_only(
            spec,
            workdir=tmp_path / "workdir",
            results_dir=tmp_path / "results",
            trials=0,
        )


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
