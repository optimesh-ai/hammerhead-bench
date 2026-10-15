"""Hammerhead coverage verdicts flow from the sidecar into scores and reports.

Hammerhead v0.2.2+ stamps a ``coverage`` verdict on every ``simulate``
answer, and ``hammerhead_stats.json`` records it (``coverage_status`` /
``coverage_degraded`` / ``coverage_blockers``; ``null`` before v0.2.2).
A degraded answer is still scored (README § 7), so every score that
includes one must say so:

- Each per-topology agreement (sim-only, frr-only-truth, with-truth) rolls
  the per-trial verdicts up into ``hammerhead_coverage_*`` fields: any
  degraded trial flags the topology; it counts as complete only when every
  trial recorded complete coverage; otherwise it is unknown.
- ``bench_summary.json`` keeps its headline means over every topology and
  adds a ``hammerhead_coverage_summary`` block: counts per verdict, the
  degraded topologies with their blockers, and the means recomputed over
  topologies with complete coverage only (``None`` when there are none,
  never a vacuous 100 %).
- The Markdown and HTML reports flag degraded topologies and list their
  blockers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from harness.adapters.frr import FrrAdapter
from harness.clab import DeployedLab
from harness.cli import main
from harness.diff.metrics import TopologyMetrics
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import (
    BenchHooks,
    FrrOnlyTruthResult,
    SimOnlyAgreement,
    ThreeWayAgreement,
    TopologyRunResult,
    aggregate_sim_only,
    run_topology,
    run_topology_frr_only_truth,
    run_topology_sim_only,
)
from harness.report.data import ReportData, TopologyRow
from harness.report.html import render_html
from harness.report.markdown import render_markdown
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"

BLOCKER = "r1: 1 unparsed forwarding-relevant line"
DEGRADED = {
    "coverage_status": "degraded",
    "coverage_degraded": True,
    "coverage_blockers": [BLOCKER],
}
COMPLETE = {
    "coverage_status": "complete",
    "coverage_degraded": False,
    "coverage_blockers": [],
}


def _fib(node: str, source: str, index: int) -> NodeFib:
    """One connected /30 per node: survives the loopback canonicalizer."""
    return NodeFib(
        node=node,
        vrf="default",
        source=source,
        routes=[
            Route(
                prefix=f"10.0.{index + 10}.0/30",
                protocol="connected",
                next_hops=[NextHop(interface="eth1")],
                admin_distance=0,
                metric=0,
            ),
        ],
    )


def _write_fibs(out_dir: Path, source: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, node in enumerate(("r1", "r2")):
        (out_dir / f"{node}__default.json").write_text(_fib(node, source, i).model_dump_json())


@dataclass
class _CoverageHook:
    """Simulator hook that writes FIBs plus a stats sidecar per call.

    ``coverage`` is the per-call sequence of coverage keys merged into the
    Hammerhead sidecar; ``None`` writes a pre-v0.2.2 sidecar with no
    coverage keys at all. The last entry repeats once the list runs out.
    """

    source: str  # "batfish" | "hammerhead"
    coverage: list[dict | None] = field(default_factory=lambda: [None])
    calls: int = 0

    def __call__(self, configs_dir: Path, out_dir: Path, topology: str) -> None:  # noqa: ARG002
        self.calls += 1
        _write_fibs(out_dir, self.source)
        payload: dict = {"simulate_s": 0.10, "total_s": 0.12, "init_snapshot_s": 0.0}
        if self.source == "hammerhead":
            payload["rib_total_s"] = 0.01
            cov = self.coverage[min(self.calls, len(self.coverage)) - 1]
            if cov is not None:
                payload.update(cov)
        (out_dir / f"{self.source}_stats.json").write_text(json.dumps(payload))


def _sim_only(tmp_path: Path, coverage: list[dict | None], trials: int):
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CoverageHook(source="batfish"),
        hammerhead=_CoverageHook(source="hammerhead", coverage=coverage),
    )
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
        trials=trials,
    )
    assert result.status == "passed", result.error
    assert result.agreement is not None
    return result


# ---- per-topology roll-up: sim-only ---------------------------------------


def test_sim_only_degraded_trials_flag_the_topology_and_are_still_scored(
    tmp_path: Path,
) -> None:
    result = _sim_only(tmp_path, [DEGRADED, DEGRADED], trials=2)
    a = result.agreement
    assert a.hammerhead_coverage_degraded is True
    assert a.hammerhead_coverage_status == "degraded"
    assert a.hammerhead_coverage_blockers == [BLOCKER]
    assert a.hammerhead_coverage_degraded_trials == 2
    assert a.hammerhead_coverage_trials == 2
    # Flagged, not dropped: the degraded answer is still diffed and scored.
    assert a.both_sides_keys == 2
    assert a.next_hop_agreement == pytest.approx(1.0)
    on_disk = json.loads(
        (tmp_path / "results" / "diff_sim_only" / "bgp-ibgp-2node" / "agreement.json").read_text()
    )
    assert on_disk["hammerhead_coverage_degraded"] is True
    assert on_disk["hammerhead_coverage_blockers"] == [BLOCKER]


def test_sim_only_one_degraded_trial_is_enough_to_flag(tmp_path: Path) -> None:
    result = _sim_only(tmp_path, [COMPLETE, DEGRADED, COMPLETE], trials=3)
    a = result.agreement
    assert a.hammerhead_coverage_degraded is True
    assert a.hammerhead_coverage_status == "degraded"
    assert a.hammerhead_coverage_blockers == [BLOCKER]
    assert a.hammerhead_coverage_degraded_trials == 1
    assert a.hammerhead_coverage_trials == 3


def test_sim_only_complete_coverage_is_recorded(tmp_path: Path) -> None:
    a = _sim_only(tmp_path, [COMPLETE], trials=1).agreement
    assert a.hammerhead_coverage_degraded is False
    assert a.hammerhead_coverage_status == "complete"
    assert a.hammerhead_coverage_blockers == []
    assert a.hammerhead_coverage_degraded_trials == 0
    assert a.hammerhead_coverage_trials == 1


def test_sim_only_pre_v022_sidecar_leaves_coverage_unknown(tmp_path: Path) -> None:
    a = _sim_only(tmp_path, [None], trials=1).agreement
    assert a.hammerhead_coverage_degraded is None
    assert a.hammerhead_coverage_status is None
    assert a.hammerhead_coverage_blockers is None
    assert a.hammerhead_coverage_degraded_trials == 0
    assert a.hammerhead_coverage_trials == 1


def test_sim_only_trial_without_a_verdict_is_not_called_complete(tmp_path: Path) -> None:
    # One complete trial and one with no verdict: nothing says the second
    # read every line, so the topology is unknown, not complete.
    a = _sim_only(tmp_path, [COMPLETE, None], trials=2).agreement
    assert a.hammerhead_coverage_degraded is None
    assert a.hammerhead_coverage_degraded_trials == 0


def test_sim_only_without_hammerhead_hook_records_no_trials(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    result = run_topology_sim_only(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=BenchHooks(batfish=_CoverageHook(source="batfish")),
    )
    assert result.status == "passed", result.error
    a = result.agreement
    assert a.hammerhead_coverage_degraded is None
    assert a.hammerhead_coverage_trials == 0


# ---- per-topology roll-up: frr-only-truth + with-truth -------------------


def _fake_truth_collector(spec, workdir, results_dir) -> None:  # noqa: ARG001
    _write_fibs(results_dir / "vendor_truth" / spec.name, "vendor")


def test_frr_only_truth_three_way_agreement_carries_coverage(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CoverageHook(source="batfish"),
        hammerhead=_CoverageHook(source="hammerhead", coverage=[DEGRADED]),
    )
    result = run_topology_frr_only_truth(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        hooks=hooks,
        truth_collector=_fake_truth_collector,
    )
    assert result.status == "passed", result.error
    a = result.three_way_agreement
    assert a is not None
    assert a.hammerhead_coverage_degraded is True
    assert a.hammerhead_coverage_blockers == [BLOCKER]
    assert a.hammerhead_coverage_degraded_trials == 1
    assert a.hammerhead_coverage_trials == 1
    # Still scored against truth.
    assert a.hammerhead_vs_truth_next_hop == pytest.approx(1.0)
    assert a.as_dict()["hammerhead_coverage_degraded"] is True


@dataclass
class _FakeClab:
    def deploy(self, topology_yaml: Path) -> DeployedLab:
        return DeployedLab(topology_yaml=topology_yaml, lab_name="hh-bench-bgp-ibgp-2node")

    def destroy(self, topology_yaml: Path) -> None:
        pass

    def dangling_resources(self) -> list[str]:
        return []


@pytest.fixture
def patched_frr_adapter(monkeypatch):
    """Vendor FIBs without docker: the same /30s the simulator hooks write."""

    def fake_wait(self, container, timeout_s=300):  # noqa: ARG001
        return True

    def fake_extract(self, container, node_name=None):  # noqa: ARG001
        node = node_name or container
        return [_fib(node, "vendor", 0 if node == "r1" else 1)]

    monkeypatch.setattr(FrrAdapter, "wait_for_convergence", fake_wait)
    monkeypatch.setattr(FrrAdapter, "extract_fib", fake_extract)


def test_with_truth_run_records_coverage(tmp_path: Path, patched_frr_adapter) -> None:  # noqa: ARG001
    spec = load_spec(TOPO_DIR)
    hooks = BenchHooks(
        batfish=_CoverageHook(source="batfish"),
        hammerhead=_CoverageHook(source="hammerhead", coverage=[DEGRADED]),
    )
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=_FakeClab(),
        hooks=hooks,
    )
    assert result.status == "passed", result.error
    assert result.metrics is not None
    assert result.hammerhead_coverage_degraded is True
    assert result.hammerhead_coverage_status == "degraded"
    assert result.hammerhead_coverage_blockers == [BLOCKER]
    assert result.hammerhead_coverage_trials == 1


# ---- corpus aggregates ---------------------------------------------------


def _agreement(
    topology: str,
    *,
    nh: float,
    degraded: bool | None,
    both: int = 10,
    blockers: list[str] | None = None,
) -> SimOnlyAgreement:
    status = {True: "degraded", False: "complete", None: None}[degraded]
    return SimOnlyAgreement(
        topology=topology,
        batfish_routes=both,
        hammerhead_routes=both,
        union_keys=both,
        both_sides_keys=both,
        next_hop_agreement=nh,
        protocol_agreement=nh,
        bgp_attr_agreement=nh,
        hammerhead_coverage_status=status,
        hammerhead_coverage_degraded=degraded,
        hammerhead_coverage_blockers=(
            blockers if blockers is not None else ([] if degraded is False else None)
        ),
        hammerhead_coverage_degraded_trials=1 if degraded else 0,
        hammerhead_coverage_trials=1,
    )


def test_aggregate_sim_only_keeps_headline_and_adds_complete_coverage_reduction() -> None:
    per = [
        _agreement("clean", nh=1.0, degraded=False),
        _agreement("gappy", nh=0.5, degraded=True, blockers=[BLOCKER]),
        _agreement("old", nh=0.8, degraded=None),
        # Complete coverage but an empty intersection: not "covered", so it
        # stays out of the covered reductions as it always has.
        _agreement("empty", nh=1.0, degraded=False, both=0),
    ]
    summary = aggregate_sim_only(per)

    # Headline means are unchanged: degraded topologies stay in them.
    assert summary["next_hop_agreement_mean"] == pytest.approx((1.0 + 0.5 + 0.8 + 1.0) / 4)
    assert summary["next_hop_agreement_mean_covered"] == pytest.approx((1.0 + 0.5 + 0.8) / 3)

    cov = summary["hammerhead_coverage_summary"]
    assert cov["complete_topology_count"] == 2
    assert cov["degraded_topology_count"] == 1
    assert cov["unknown_topology_count"] == 1
    assert cov["degraded_topologies"] == [
        {
            "topology": "gappy",
            "status": "degraded",
            "blockers": [BLOCKER],
            "degraded_trials": 1,
            "trials": 1,
        }
    ]
    # Covered AND complete coverage: only "clean".
    assert cov["covered_complete_topology_count"] == 1
    assert cov["next_hop_agreement_mean_covered_complete"] == pytest.approx(1.0)
    assert cov["protocol_agreement_mean_covered_complete"] == pytest.approx(1.0)
    assert cov["bgp_attr_agreement_mean_covered_complete"] == pytest.approx(1.0)


def test_aggregate_sim_only_complete_reduction_is_none_without_complete_topologies() -> None:
    # An all-pre-v0.2.2 corpus must not report a vacuous 100 %.
    summary = aggregate_sim_only(
        [_agreement("a", nh=0.4, degraded=None), _agreement("b", nh=0.6, degraded=True)]
    )
    cov = summary["hammerhead_coverage_summary"]
    assert cov["complete_topology_count"] == 0
    assert cov["covered_complete_topology_count"] == 0
    assert cov["next_hop_agreement_mean_covered_complete"] is None
    assert cov["protocol_agreement_mean_covered_complete"] is None
    assert cov["bgp_attr_agreement_mean_covered_complete"] is None


def _metrics(topology: str, hh_nh: float) -> TopologyMetrics:
    return TopologyMetrics(
        topology=topology,
        total_routes_vendor=4,
        total_routes_batfish=4,
        total_routes_hammerhead=4,
        batfish_presence_match_rate=1.0,
        batfish_next_hop_match_rate=1.0,
        batfish_protocol_match_rate=1.0,
        batfish_bgp_attr_match_rate=1.0,
        batfish_per_protocol_next_hop_match_rate={},
        hammerhead_presence_match_rate=hh_nh,
        hammerhead_next_hop_match_rate=hh_nh,
        hammerhead_protocol_match_rate=hh_nh,
        hammerhead_bgp_attr_match_rate=hh_nh,
        hammerhead_per_protocol_next_hop_match_rate={},
    )


def test_with_truth_bench_summary_flags_degraded_topologies(
    tmp_path: Path, monkeypatch
) -> None:
    canned = {
        "bgp-ibgp-2node": (0.5, True, [BLOCKER]),
        "bgp-ebgp-2node": (1.0, False, []),
    }

    def fake_run_topology(spec, *, workdir, results_dir, **kwargs) -> TopologyRunResult:  # noqa: ARG001
        nh, degraded, blockers = canned[spec.name]
        return TopologyRunResult(
            topology=spec.name,
            status="passed",
            started_iso="2026-10-15T00:00:00+00:00",
            finished_iso="2026-10-15T00:00:01+00:00",
            metrics=_metrics(spec.name, nh),
            hammerhead_coverage_status="degraded" if degraded else "complete",
            hammerhead_coverage_degraded=degraded,
            hammerhead_coverage_blockers=blockers,
            hammerhead_coverage_degraded_trials=1 if degraded else 0,
            hammerhead_coverage_trials=1,
        )

    monkeypatch.setattr("harness.cli.run_topology", fake_run_topology)
    res = CliRunner().invoke(
        main,
        [
            "bench",
            "--only", "bgp-ibgp-2node",
            "--only", "bgp-ebgp-2node",
            "--trials", "1",
            "--results-dir", str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    summary = json.loads((tmp_path / "bench_summary.json").read_text())
    # Headline unchanged: both topologies count.
    assert summary["hammerhead_next_hop_match_rate_mean"] == pytest.approx(0.75)
    cov = summary["hammerhead_coverage_summary"]
    assert cov["complete_topology_count"] == 1
    assert cov["degraded_topology_count"] == 1
    assert [d["topology"] for d in cov["degraded_topologies"]] == ["bgp-ibgp-2node"]
    assert cov["hammerhead_next_hop_match_rate_mean_complete"] == pytest.approx(1.0)
    run = json.loads((tmp_path / "bgp-ibgp-2node.json").read_text())
    assert run["hammerhead_coverage_degraded"] is True
    assert run["hammerhead_coverage_blockers"] == [BLOCKER]


def test_frr_only_truth_bench_summary_flags_degraded_topologies(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_run(spec, *, workdir, results_dir, hooks, **kwargs) -> FrrOnlyTruthResult:  # noqa: ARG001
        return FrrOnlyTruthResult(
            topology=spec.name,
            status="passed",
            started_iso="2026-10-15T00:00:00+00:00",
            finished_iso="2026-10-15T00:00:01+00:00",
            truth_source="containerlab-frr",
            three_way_agreement=ThreeWayAgreement(
                topology=spec.name,
                hammerhead_coverage_status="degraded",
                hammerhead_coverage_degraded=True,
                hammerhead_coverage_blockers=[BLOCKER],
                hammerhead_coverage_degraded_trials=1,
                hammerhead_coverage_trials=1,
            ),
        )

    monkeypatch.setattr("harness.cli.run_topology_frr_only_truth", fake_run)
    res = CliRunner().invoke(
        main,
        [
            "bench",
            "--frr-only-truth",
            "--only", "bgp-ibgp-2node",
            "--trials", "1",
            "--results-dir", str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    summary = json.loads((tmp_path / "bench_summary.json").read_text())
    cov = summary["hammerhead_coverage_summary"]
    assert cov["degraded_topology_count"] == 1
    assert cov["degraded_topologies"][0]["blockers"] == [BLOCKER]


# ---- reports -------------------------------------------------------------


def _sim_only_report(tmp_path: Path, per: list[SimOnlyAgreement]) -> ReportData:
    summary = aggregate_sim_only(per)
    summary["mode"] = "sim_only"
    summary["failed_topologies"] = []
    rows = [
        TopologyRow(
            topology=a.topology,
            run={"topology": a.topology, "status": "passed", "mode": "sim_only",
                 "agreement": a.as_dict()},
        )
        for a in per
    ]
    return ReportData(results_dir=tmp_path, summary=summary, topologies=rows)


def test_markdown_flags_degraded_topologies_and_lists_blockers(tmp_path: Path) -> None:
    data = _sim_only_report(
        tmp_path,
        [
            _agreement("clean", nh=1.0, degraded=False),
            _agreement("gappy", nh=0.5, degraded=True, blockers=[BLOCKER]),
            _agreement("old", nh=0.8, degraded=None),
        ],
    )
    md = render_markdown(data)

    # Per-topology rows carry the verdict.
    gappy_row = next(line for line in md.splitlines() if line.startswith("| gappy |"))
    assert "degraded" in gappy_row
    clean_row = next(line for line in md.splitlines() if line.startswith("| clean |"))
    assert "complete" in clean_row

    # A coverage section names the degraded topology and its blockers.
    assert "## Hammerhead coverage" in md
    section = md.split("## Hammerhead coverage", 1)[1].split("\n## ", 1)[0]
    assert "gappy" in section
    assert BLOCKER in section
    assert "clean" not in section

    # The headline carries the complete-coverage reduction with its count.
    headline = md.split("## Per-topology", 1)[0]
    assert "coverage complete" in headline
    assert "100.0%" in headline  # nh over "clean" only


def test_markdown_omits_coverage_section_when_nothing_recorded(tmp_path: Path) -> None:
    data = _sim_only_report(tmp_path, [_agreement("old", nh=0.8, degraded=None)])
    md = render_markdown(data)
    assert "## Hammerhead coverage" not in md


def test_markdown_truth_section_flags_degraded_topology(tmp_path: Path) -> None:
    three_way = ThreeWayAgreement(
        topology="alpha",
        hammerhead_coverage_status="degraded",
        hammerhead_coverage_degraded=True,
        hammerhead_coverage_blockers=[BLOCKER],
        hammerhead_coverage_degraded_trials=1,
        hammerhead_coverage_trials=1,
    ).as_dict()
    run = {
        "topology": "alpha",
        "status": "passed",
        "mode": "frr_only_truth",
        "truth_source": "containerlab-frr",
        "three_way_agreement": three_way,
        "agreement": three_way,
    }
    data = ReportData(
        results_dir=tmp_path,
        summary={"mode": "frr_only_truth", "topology_count": 1},
        topologies=[TopologyRow(topology="alpha", run=run)],
    )
    md = render_markdown(data)
    truth = md.split("## Ground-truth agreement (FRR subset)", 1)[1]
    alpha_row = next(line for line in truth.splitlines() if line.startswith("| alpha |"))
    assert "degraded" in alpha_row
    assert BLOCKER in md.split("## Hammerhead coverage", 1)[1]


def test_html_lists_degraded_topologies_with_escaped_blockers(tmp_path: Path) -> None:
    blocker = "r1: <unparsed> line"
    data = _sim_only_report(
        tmp_path, [_agreement("gappy", nh=0.5, degraded=True, blockers=[blocker])]
    )
    out = render_html(data)
    assert "Hammerhead coverage" in out
    assert "gappy" in out
    assert "r1: &lt;unparsed&gt; line" in out
    assert "<unparsed>" not in out
