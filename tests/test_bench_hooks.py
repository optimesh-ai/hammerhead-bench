"""Tests for ``BenchHooks`` wiring through ``run_topology``.

Fakes replace both the vendor extractor (no docker) and the batfish +
hammerhead hooks. Each fake hook writes the exact same NodeFib that the
vendor extractor returned for each node — so the post-hook diff engine
sees a three-way all-match workspace.

Covered:

- Hook paths land on ``TopologyRunResult`` when hooks fire.
- Both hooks skipped → no diff computed, no stale paths populated.
- Diff files (records.json, metrics.json) land on disk when hooks fire.
- ``filter_loopback_host`` threads through to the diff engine (smoke: set
  it to False and confirm the metrics row count increases because the
  connected /32 for r1's lo shows up in the roll-up).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harness.adapters.frr import FrrAdapter
from harness.clab import DeployedLab
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import BenchHooks, run_topology
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"


def _vendor_fib_for(node: str) -> list[NodeFib]:
    """Synthetic vendor FIB for node r1 or r2. Each carries:

    - A connected /32 for its own loopback (filtered out by default).
    - A BGP /32 learned from the peer over iBGP.
    """
    other = "r2" if node == "r1" else "r1"
    self_lo = "10.0.0.1" if node == "r1" else "10.0.0.2"
    peer_lo = "10.0.0.2" if node == "r1" else "10.0.0.1"
    peer_nh = "10.0.12.2" if node == "r1" else "10.0.12.1"
    return [
        NodeFib(
            node=node,
            vrf="default",
            source="vendor",
            routes=[
                Route(
                    prefix=f"{self_lo}/32",
                    protocol="connected",
                    next_hops=[NextHop(interface="lo")],
                    admin_distance=0,
                    metric=0,
                ),
                Route(
                    prefix=f"{peer_lo}/32",
                    protocol="bgp",
                    next_hops=[NextHop(ip=peer_nh, interface="eth1")],
                    admin_distance=200,
                    metric=0,
                    local_pref=100,
                    as_path=[],
                    med=None,
                ),
            ],
        )
    ]
    _ = other  # silence unused-var (reserved for future many-node variant)


@dataclass
class _FakeClab:
    deployed_paths: list[Path] = field(default_factory=list)
    destroyed_paths: list[Path] = field(default_factory=list)

    def deploy(self, topology_yaml: Path) -> DeployedLab:
        self.deployed_paths.append(topology_yaml)
        return DeployedLab(topology_yaml=topology_yaml, lab_name="hh-bench-bgp-ibgp-2node")

    def destroy(self, topology_yaml: Path) -> None:
        self.destroyed_paths.append(topology_yaml)

    def dangling_resources(self) -> list[str]:
        return []


@pytest.fixture
def patched_adapter(monkeypatch):
    """Stub FrrAdapter convergence + extract with a synthetic two-route FIB."""

    def fake_wait(self, container, timeout_s=300):  # noqa: ARG001
        return True

    def fake_extract(self, container, node_name=None):  # noqa: ARG001
        return _vendor_fib_for(node_name or container)

    monkeypatch.setattr(FrrAdapter, "wait_for_convergence", fake_wait)
    monkeypatch.setattr(FrrAdapter, "extract_fib", fake_extract)


def _write_sim_fib(out_dir: Path, node: str, source: str) -> None:
    """Mirror _vendor_fib_for, but stamp the ``source`` field so the diff
    engine knows which simulator this came from. Writes to the layout
    ``<out_dir>/<node>__<vrf>.json`` that ``load_fib_workspace`` expects."""
    [fib] = _vendor_fib_for(node)
    stamped = fib.model_copy(update={"source": source})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{node}__{fib.vrf}.json").write_text(stamped.model_dump_json())


def _fake_batfish_hook(configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG001
    for node in ("r1", "r2"):
        _write_sim_fib(out_dir, node, "batfish")


def _fake_hammerhead_hook(configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG001
    for node in ("r1", "r2"):
        _write_sim_fib(out_dir, node, "hammerhead")


# ----- the actual tests ---------------------------------------------------


def test_both_hooks_fire_populate_paths_on_result(tmp_path: Path, patched_adapter) -> None:  # noqa: ARG001
    spec = load_spec(TOPO_DIR)
    clab = _FakeClab()
    hooks = BenchHooks(batfish=_fake_batfish_hook, hammerhead=_fake_hammerhead_hook)
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
        hooks=hooks,
    )

    assert result.status == "passed", result.error
    assert result.vendor_truth_path is not None
    assert result.batfish_path is not None
    assert result.hammerhead_path is not None
    assert result.diff_path is not None
    assert result.metrics is not None


def test_no_hooks_skips_diff_and_leaves_simulator_paths_none(
    tmp_path: Path, patched_adapter  # noqa: ARG001
) -> None:
    spec = load_spec(TOPO_DIR)
    clab = _FakeClab()
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
        # no hooks=... → default BenchHooks() → both None
    )

    assert result.status == "passed", result.error
    assert result.vendor_truth_path is not None
    assert result.batfish_path is None
    assert result.hammerhead_path is None
    assert result.diff_path is None
    assert result.metrics is None
    # No per-simulator directories should have been created.
    assert not (tmp_path / "results" / "batfish").exists()
    assert not (tmp_path / "results" / "hammerhead").exists()
    assert not (tmp_path / "results" / "diff").exists()


def test_hooks_produce_diff_records_and_metrics_on_disk(
    tmp_path: Path, patched_adapter  # noqa: ARG001
) -> None:
    spec = load_spec(TOPO_DIR)
    clab = _FakeClab()
    hooks = BenchHooks(batfish=_fake_batfish_hook, hammerhead=_fake_hammerhead_hook)
    run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
        hooks=hooks,
    )

    diff_dir = tmp_path / "results" / "diff" / "bgp-ibgp-2node"
    records_path = diff_dir / "records.json"
    metrics_path = diff_dir / "metrics.json"
    assert records_path.exists()
    assert metrics_path.exists()

    records = json.loads(records_path.read_text())
    # Identical FIBs across all three sources → at minimum the peer BGP /32
    # for both r1 and r2 lands in the diff (connected /32 is filtered by
    # default via filter_loopback_host).
    assert isinstance(records, list)
    assert len(records) >= 2
    for rec in records:
        assert rec["presence"] == "all-three"

    metrics = json.loads(metrics_path.read_text())
    # Perfect match across all sources → every roll-up rate is 1.0.
    assert metrics["batfish_next_hop_match_rate"] == 1.0
    assert metrics["hammerhead_next_hop_match_rate"] == 1.0
    assert metrics["batfish_presence_match_rate"] == 1.0
    assert metrics["hammerhead_presence_match_rate"] == 1.0


def test_filter_loopback_host_false_includes_connected_slash32(
    tmp_path: Path, patched_adapter  # noqa: ARG001
) -> None:
    spec = load_spec(TOPO_DIR)
    clab = _FakeClab()

    hooks_filtered = BenchHooks(
        batfish=_fake_batfish_hook,
        hammerhead=_fake_hammerhead_hook,
        filter_loopback_host=True,
    )
    run_topology(
        spec,
        workdir=tmp_path / "workdir-filt",
        results_dir=tmp_path / "results-filt",
        clab=clab,
        hooks=hooks_filtered,
    )
    filt_records = json.loads(
        (tmp_path / "results-filt" / "diff" / "bgp-ibgp-2node" / "records.json").read_text()
    )

    hooks_unfiltered = BenchHooks(
        batfish=_fake_batfish_hook,
        hammerhead=_fake_hammerhead_hook,
        filter_loopback_host=False,
    )
    run_topology(
        spec,
        workdir=tmp_path / "workdir-unfilt",
        results_dir=tmp_path / "results-unfilt",
        clab=_FakeClab(),
        hooks=hooks_unfiltered,
    )
    unfilt_records = json.loads(
        (tmp_path / "results-unfilt" / "diff" / "bgp-ibgp-2node" / "records.json").read_text()
    )

    assert len(unfilt_records) > len(filt_records), (
        f"unfiltered={len(unfilt_records)}, filtered={len(filt_records)}"
    )
