"""Tests for the ``--frr-only-truth`` mode (Issue 4).

Covers:

- :func:`harness.topology.frr_only_truth_eligible` — acceptance on small
  FRR-only topologies, rejection on large or non-FRR topologies.
- :func:`harness.pipeline.run_topology_frr_only_truth` — fallback to the
  sim-only path when the topology is ineligible (MOCK truth collector
  would raise if it were called).
- :class:`harness.pipeline.ThreeWayAgreement` — JSON shape + backward-
  compat (sim-only fallback path carries no truth fields).
- Markdown renderer gating: section absent when no topology has truth;
  present + rows render when at least one does.
- CLI mutual-exclusion: ``--sim-only`` and ``--frr-only-truth`` together
  exit non-zero with a clear message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from harness.adapters.ceos import CeosAdapter
from harness.adapters.frr import FrrAdapter
from harness.cli import main
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import (
    BenchHooks,
    SimOnlyAgreement,
    ThreeWayAgreement,
    run_topology_frr_only_truth,
)
from harness.report.data import ReportData, TopologyRow
from harness.report.markdown import render_markdown
from harness.topology import (
    FRR_ONLY_TRUTH_MAX_NODES,
    Interface,
    Link,
    Node,
    TopologySpec,
    frr_only_truth_eligible,
    load_spec,
)

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"
MIXED_TOPO_DIR = (
    Path(__file__).resolve().parent.parent / "topologies" / "mixed-vendor-frr-ceos-4node"
)


# ---- helpers -------------------------------------------------------------


def _tiny_frr_spec(name: str, *, nodes: int = 2) -> TopologySpec:
    """Build a synthetic FRR-only topology. Templates point at a real dir so
    any accidental rendering doesn't explode on a missing path; the tests
    here never actually render unless they go through the eligible pipeline
    path, which the MOCK truth collector short-circuits."""
    frr = FrrAdapter()
    nodes_tuple = tuple(
        Node(
            name=f"r{i + 1}",
            adapter=frr,
            interfaces=(Interface(name="eth1", ip=f"10.0.0.{i + 1}/32"),),
            params={"asn": 65000 + i},
        )
        for i in range(nodes)
    )
    return TopologySpec(
        name=name,
        description=f"synthetic {nodes}-node FRR topology",
        template_dir=TOPO_DIR / "templates",  # reuse real dir so Path exists
        nodes=nodes_tuple,
        links=(),
    )


def _synthetic_mixed_frr_ceos_spec() -> TopologySpec:
    """Synthetic 2-node topology: one FRR node + one cEOS node — disqualifies.

    Used for pure-eligibility assertions — never rendered (the template dir
    references a real dir so ``Path`` doesn't explode, but the test paths
    that exercise this spec stop before ``render_topology`` ever fires).
    """
    return TopologySpec(
        name="synthetic-mixed",
        description="synthetic FRR + cEOS",
        template_dir=TOPO_DIR / "templates",
        nodes=(
            Node(
                name="r1",
                adapter=FrrAdapter(),
                interfaces=(Interface(name="eth1", ip="10.0.0.1/32"),),
                params={},
            ),
            Node(
                name="sw1",
                adapter=CeosAdapter(),
                interfaces=(Interface(name="eth1", ip="10.0.0.2/32"),),
                params={},
            ),
        ),
        links=(Link(a=("r1", "eth1"), b=("sw1", "eth1")),),
    )


@dataclass
class _CountingTruthCollector:
    """MOCK truth collector that records whether it was called.

    Used in fallback tests to prove the ineligible-topology path does NOT
    invoke the collector (truth collection is expensive; we don't want to
    accidentally trigger it on a non-eligible topology).
    """

    calls: int = 0
    raise_if_called: bool = False

    def __call__(self, spec, workdir, results_dir) -> None:  # noqa: ARG002
        self.calls += 1
        if self.raise_if_called:
            raise AssertionError(
                "truth collector was called on an ineligible topology"
            )


@dataclass
class _StampingHook:
    """Hook that stamps hand-built FIBs into ``out_dir`` per trial.

    Used for the sim-only fallback test so the pipeline can complete end-
    to-end without a real Batfish / Hammerhead binary. Emits one /30 transit
    route per node in ``nodes`` (NOT a /32 lo route — the canonicalizer
    filters /32 connected-on-lo entries when ``filter_loopback_host=True``,
    which is the default).
    """

    source: str
    nodes: tuple[str, ...] = ("r1", "r2")
    calls: int = field(default=0)

    def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
        self.calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(self.nodes):
            fib = NodeFib(
                node=name,
                vrf="default",
                source=self.source,
                routes=[
                    Route(
                        prefix=f"10.0.{i + 10}.0/30",
                        protocol="connected",
                        next_hops=[NextHop(interface="eth1")],
                        admin_distance=0,
                        metric=0,
                    ),
                ],
            )
            (out_dir / f"{name}__default.json").write_text(fib.model_dump_json())
        sidecar = out_dir / f"{self.source}_stats.json"
        sidecar.write_text(json.dumps({"total_s": 0.12}))


# ---- eligibility ---------------------------------------------------------


def test_frr_only_truth_eligible_accepts_frr_small_topology() -> None:
    spec = _tiny_frr_spec("frr-small", nodes=2)
    assert frr_only_truth_eligible(spec) is True
    # The real 2-node iBGP topology should also qualify.
    real = load_spec(TOPO_DIR)
    assert frr_only_truth_eligible(real) is True


def test_frr_only_truth_eligible_rejects_large_topology() -> None:
    # FRR-only but > FRR_ONLY_TRUTH_MAX_NODES.
    assert FRR_ONLY_TRUTH_MAX_NODES == 20
    spec = _tiny_frr_spec("frr-huge", nodes=FRR_ONLY_TRUTH_MAX_NODES + 1)
    assert frr_only_truth_eligible(spec) is False
    # Boundary: exactly FRR_ONLY_TRUTH_MAX_NODES is still eligible.
    boundary = _tiny_frr_spec("frr-boundary", nodes=FRR_ONLY_TRUTH_MAX_NODES)
    assert frr_only_truth_eligible(boundary) is True


def test_frr_only_truth_eligible_rejects_non_frr_vendor() -> None:
    # Mixed FRR+cEOS → disqualifies even though both nodes are small.
    spec = _synthetic_mixed_frr_ceos_spec()
    assert frr_only_truth_eligible(spec) is False
    # The real 4-node mixed-vendor topology should also be disqualified.
    real_mixed = load_spec(MIXED_TOPO_DIR)
    assert frr_only_truth_eligible(real_mixed) is False


def test_frr_only_truth_eligible_rejects_external_renderer() -> None:
    # External-renderer topologies skip the Jinja + clab YAML path — they
    # can't be deployed via containerlab, so they must fall back to sim-only.
    base = _tiny_frr_spec("frr-external", nodes=2)
    spec = TopologySpec(
        name=base.name,
        description=base.description,
        template_dir=base.template_dir,
        nodes=base.nodes,
        links=base.links,
        external_renderer=lambda _cd: None,
    )
    assert frr_only_truth_eligible(spec) is False


# ---- pipeline fallback ---------------------------------------------------


def test_frr_only_truth_falls_back_to_sim_only_when_ineligible(tmp_path: Path) -> None:
    """An ineligible topology must NOT invoke the truth collector and MUST
    produce a result with ``truth_source is None`` that still carries the
    B-vs-H sim-only agreement."""
    # Use the real mixed-vendor (FRR+cEOS) topology so ``render_topology``
    # has working templates. Eligibility fails on vendor diversity.
    spec = load_spec(MIXED_TOPO_DIR)
    assert frr_only_truth_eligible(spec) is False
    node_names = tuple(n.name for n in spec.nodes)
    truth = _CountingTruthCollector(raise_if_called=True)
    hooks = BenchHooks(
        batfish=_StampingHook(source="batfish", nodes=node_names),
        hammerhead=_StampingHook(source="hammerhead", nodes=node_names),
    )
    result = run_topology_frr_only_truth(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
        truth_collector=truth,
    )
    # Fallback path: collector NEVER called.
    assert truth.calls == 0
    # Status is passed (sim-only completes fine).
    assert result.status == "passed", result.error
    # Primary discriminator: no truth source.
    assert result.truth_source is None
    # Sim-only agreement present, three-way absent.
    assert result.sim_only_agreement is not None
    assert result.three_way_agreement is None
    # Both stamping hooks produced one route per node → equal counts.
    assert result.sim_only_agreement.batfish_routes == len(node_names)
    assert result.sim_only_agreement.hammerhead_routes == len(node_names)


def test_frr_only_truth_eligible_invokes_truth_collector(tmp_path: Path) -> None:
    """The eligible path wires the collector + exposes three-way agreement.

    We stamp all three FIB sources identically, so every triad should be 100%.
    """
    spec = load_spec(TOPO_DIR)  # real bgp-ibgp-2node, FRR-only, 2 nodes
    assert frr_only_truth_eligible(spec) is True

    def _fake_truth_collector(spec, workdir, results_dir) -> None:  # noqa: ARG001
        vt_dir = results_dir / "vendor_truth" / spec.name
        vt_dir.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(("r1", "r2")):
            fib = NodeFib(
                node=name,
                vrf="default",
                source="vendor",
                routes=[
                    Route(
                        prefix=f"10.0.{i + 10}.0/30",
                        protocol="connected",
                        next_hops=[NextHop(interface="eth1")],
                        admin_distance=0,
                        metric=0,
                    ),
                ],
            )
            (vt_dir / f"{name}__default.json").write_text(fib.model_dump_json())

    hooks = BenchHooks(
        batfish=_StampingHook(source="batfish"),
        hammerhead=_StampingHook(source="hammerhead"),
    )
    result = run_topology_frr_only_truth(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
        truth_collector=_fake_truth_collector,
    )
    assert result.status == "passed", result.error
    assert result.truth_source == "containerlab-frr"
    assert result.three_way_agreement is not None
    assert result.sim_only_agreement is None
    a = result.three_way_agreement
    assert a.truth_routes == 2
    assert a.batfish_routes == 2
    assert a.hammerhead_routes == 2
    # All three triads are stamped-identical → 100% presence + nh.
    assert a.batfish_vs_truth_presence == pytest.approx(1.0)
    assert a.hammerhead_vs_truth_presence == pytest.approx(1.0)
    assert a.batfish_vs_hammerhead_presence == pytest.approx(1.0)
    assert a.batfish_vs_truth_next_hop == pytest.approx(1.0)
    assert a.hammerhead_vs_truth_next_hop == pytest.approx(1.0)


# ---- ThreeWayAgreement shape --------------------------------------------


def test_three_way_agreement_dataclass_as_dict_carries_truth_fields() -> None:
    a = ThreeWayAgreement(
        topology="toy",
        truth_source="containerlab-frr",
        truth_routes=10,
        batfish_routes=9,
        hammerhead_routes=10,
        batfish_vs_truth_both_keys=9,
        batfish_vs_truth_union_keys=10,
        batfish_vs_truth_presence=0.9,
        batfish_vs_truth_next_hop=1.0,
        batfish_vs_truth_protocol=1.0,
        batfish_vs_truth_bgp_attr=1.0,
        hammerhead_vs_truth_both_keys=10,
        hammerhead_vs_truth_union_keys=10,
        hammerhead_vs_truth_presence=1.0,
        hammerhead_vs_truth_next_hop=1.0,
        hammerhead_vs_truth_protocol=1.0,
        hammerhead_vs_truth_bgp_attr=1.0,
        batfish_vs_hammerhead_both_keys=9,
        batfish_vs_hammerhead_union_keys=10,
        batfish_vs_hammerhead_presence=0.9,
        batfish_vs_hammerhead_next_hop=1.0,
        batfish_vs_hammerhead_protocol=1.0,
        batfish_vs_hammerhead_bgp_attr=1.0,
    )
    d = a.as_dict()
    # Truth fields present at the top level.
    assert d["truth_routes"] == 10
    assert d["truth_source"] == "containerlab-frr"
    # All three triads carry the expected 4 metrics each.
    for prefix in ("batfish_vs_truth", "hammerhead_vs_truth", "batfish_vs_hammerhead"):
        for field_name in ("presence", "next_hop", "protocol", "bgp_attr"):
            assert f"{prefix}_{field_name}" in d, f"missing {prefix}_{field_name}"
    # Presence aliased to ``coverage`` for renderer uniformity.
    assert d["batfish_vs_truth_coverage"] == pytest.approx(0.9)
    assert d["hammerhead_vs_truth_coverage"] == pytest.approx(1.0)
    assert d["batfish_vs_hammerhead_coverage"] == pytest.approx(0.9)


def test_three_way_agreement_omits_truth_fields_when_none() -> None:
    """Backward-compat: a sim-only-only result dict has no spurious truth keys.

    Ensures the ``--sim-only`` / pre-issue-4 code path keeps producing JSON
    byte-identical to before. We exercise this by constructing a
    :class:`SimOnlyAgreement` — its ``as_dict()`` must NOT carry any of the
    three-way triads.
    """
    a = SimOnlyAgreement(
        topology="toy",
        batfish_routes=4,
        hammerhead_routes=4,
        union_keys=4,
        both_sides_keys=4,
        next_hop_agreement=1.0,
        protocol_agreement=1.0,
        bgp_attr_agreement=1.0,
    )
    d = a.as_dict()
    for forbidden in (
        "truth_routes",
        "truth_source",
        "batfish_vs_truth_presence",
        "hammerhead_vs_truth_presence",
        "batfish_vs_hammerhead_presence",
    ):
        assert forbidden not in d, f"unexpected {forbidden} in sim-only agreement"


# ---- markdown renderer --------------------------------------------------


def _run_blob_sim_only(topology: str) -> dict:
    """Minimal ``results/<topology>.json`` shape for a sim-only row."""
    return {
        "topology": topology,
        "status": "passed",
        "mode": "sim_only",
        "truth_source": None,
        "agreement": {
            "batfish_routes": 4,
            "hammerhead_routes": 4,
            "both_sides_keys": 4,
            "union_keys": 4,
            "presence": 1.0,
            "coverage": 1.0,
            "next_hop_agreement": 1.0,
            "protocol_agreement": 1.0,
            "bgp_attr_agreement": 1.0,
        },
    }


def _run_blob_with_truth(topology: str) -> dict:
    return {
        "topology": topology,
        "status": "passed",
        "mode": "frr_only_truth",
        "truth_source": "containerlab-frr",
        "three_way_agreement": {
            "truth_routes": 6,
            "batfish_routes": 6,
            "hammerhead_routes": 6,
            "batfish_vs_truth_presence": 1.0,
            "batfish_vs_truth_next_hop": 1.0,
            "hammerhead_vs_truth_presence": 1.0,
            "hammerhead_vs_truth_next_hop": 1.0,
            "batfish_vs_hammerhead_presence": 1.0,
            "batfish_vs_hammerhead_next_hop": 1.0,
        },
        "agreement": {
            "batfish_routes": 6,
            "hammerhead_routes": 6,
        },
    }


def test_markdown_renderer_omits_truth_section_when_no_truth(tmp_path: Path) -> None:
    data = ReportData(
        results_dir=tmp_path,
        summary={"mode": "sim_only", "topology_count": 1},
        topologies=[
            TopologyRow(topology="alpha", run=_run_blob_sim_only("alpha"), metrics=None),
        ],
    )
    md = render_markdown(data)
    assert "Ground-truth agreement" not in md


def test_markdown_renderer_emits_truth_section_when_any_topology_has_truth(
    tmp_path: Path,
) -> None:
    data = ReportData(
        results_dir=tmp_path,
        summary={"mode": "frr_only_truth", "topology_count": 2},
        topologies=[
            TopologyRow(topology="alpha", run=_run_blob_with_truth("alpha"), metrics=None),
            TopologyRow(topology="beta", run=_run_blob_sim_only("beta"), metrics=None),
        ],
    )
    md = render_markdown(data)
    assert "## Ground-truth agreement (FRR subset)" in md
    # Alpha (has truth) renders as a row; beta (no truth) does NOT appear in
    # the truth section.
    truth_split = md.split("## Ground-truth agreement (FRR subset)", 1)[1]
    assert "| alpha |" in truth_split
    # The ineligible topology is intentionally absent from the truth section
    # (its numbers are null there).
    assert "| beta |" not in truth_split.split("## Failed", 1)[0]


# ---- CLI mutual-exclusion ------------------------------------------------


def test_bench_cli_rejects_both_sim_only_and_frr_only_truth() -> None:
    """``--sim-only`` and ``--frr-only-truth`` are mutually exclusive; the CLI
    exits non-zero with a clear message when both are set."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "bench",
            "--sim-only",
            "--frr-only-truth",
            "--only",
            "bgp-ibgp-2node",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "mutually exclusive" in combined
    assert "--sim-only" in combined
    assert "--frr-only-truth" in combined
