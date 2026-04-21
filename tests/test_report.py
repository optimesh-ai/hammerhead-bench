"""Report generator tests — data loader, Markdown, HTML, Plotly figures.

Covers happy path (multi-topology run with one failed + one passed),
degenerate paths (empty results dir, topology with no metrics), and
schema invariants (HTML is well-formed, Plotly figures serialize).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness.diff.metrics import TopologyMetrics
from harness.report.data import load_results
from harness.report.html import render_html, render_html_report
from harness.report.markdown import render_markdown, render_markdown_report
from harness.report.plots import match_rate_bar, per_protocol_bar, presence_bar

# ---- fixtures ------------------------------------------------------------


def _write_run(
    results_dir: Path,
    topology: str,
    *,
    status: str = "passed",
    error: str | None = None,
    notes: list[str] | None = None,
) -> None:
    payload = {
        "topology": topology,
        "status": status,
        "started_iso": "2026-04-18T00:00:00+00:00",
        "finished_iso": "2026-04-18T00:00:30+00:00",
        "vendor_truth_path": str(results_dir / "vendor_truth" / topology),
        "error": error,
        "notes": notes or [],
    }
    (results_dir / f"{topology}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _write_metrics(results_dir: Path, topology: str, **overrides: object) -> None:
    metrics = TopologyMetrics(
        topology=topology,
        total_routes_vendor=40,
        total_routes_batfish=38,
        total_routes_hammerhead=40,
        batfish_presence_match_rate=0.95,
        batfish_next_hop_match_rate=0.92,
        batfish_protocol_match_rate=0.94,
        batfish_bgp_attr_match_rate=0.91,
        batfish_per_protocol_next_hop_match_rate={"bgp": 0.90, "ospf": 0.99, "connected": 1.0},
        hammerhead_presence_match_rate=1.0,
        hammerhead_next_hop_match_rate=0.995,
        hammerhead_protocol_match_rate=0.99,
        hammerhead_bgp_attr_match_rate=0.98,
        hammerhead_per_protocol_next_hop_match_rate={"bgp": 0.98, "ospf": 1.0, "connected": 1.0},
    )
    # Apply overrides to mimic "varied" topologies in the same fixture.
    for k, v in overrides.items():
        setattr(metrics, k, v)
    diff_dir = results_dir / "diff" / topology
    diff_dir.mkdir(parents=True, exist_ok=True)
    (diff_dir / "metrics.json").write_text(json.dumps(metrics.as_dict(), indent=2) + "\n")


def _write_summary(results_dir: Path, failed: list[str] | None = None) -> None:
    payload = {
        "topology_count": 2,
        "batfish_presence_match_rate_mean": 0.92,
        "batfish_next_hop_match_rate_mean": 0.88,
        "batfish_protocol_match_rate_mean": 0.93,
        "batfish_bgp_attr_match_rate_mean": 0.85,
        "hammerhead_presence_match_rate_mean": 1.0,
        "hammerhead_next_hop_match_rate_mean": 0.995,
        "hammerhead_protocol_match_rate_mean": 0.99,
        "hammerhead_bgp_attr_match_rate_mean": 0.98,
        "failed_topologies": failed or [],
    }
    (results_dir / "bench_summary.json").write_text(json.dumps(payload, indent=2) + "\n")


@pytest.fixture()
def results_dir_two_topologies(tmp_path: Path) -> Path:
    _write_run(tmp_path, "bgp-ibgp-2node", status="passed")
    _write_metrics(tmp_path, "bgp-ibgp-2node")
    _write_run(tmp_path, "ospf-p2p-3node", status="passed")
    _write_metrics(
        tmp_path,
        "ospf-p2p-3node",
        batfish_next_hop_match_rate=0.83,
        hammerhead_next_hop_match_rate=0.995,
        batfish_per_protocol_next_hop_match_rate={"ospf": 0.83, "connected": 1.0},
        hammerhead_per_protocol_next_hop_match_rate={"ospf": 0.99, "connected": 1.0},
    )
    _write_summary(tmp_path)
    return tmp_path


@pytest.fixture()
def results_dir_with_failure(tmp_path: Path) -> Path:
    _write_run(tmp_path, "bgp-ibgp-2node", status="passed")
    _write_metrics(tmp_path, "bgp-ibgp-2node")
    _write_run(
        tmp_path,
        "route-reflector-6node",
        status="failed",
        error="batfish: timed out on deploy",
        notes=["convergence never achieved", "teardown succeeded"],
    )
    _write_summary(tmp_path, failed=["route-reflector-6node"])
    return tmp_path


# ---- data loader ---------------------------------------------------------


def test_load_results_finds_all_three_artifacts(results_dir_two_topologies: Path) -> None:
    data = load_results(results_dir_two_topologies)
    assert data.summary["topology_count"] == 2
    assert [row.topology for row in data.topologies] == sorted(["bgp-ibgp-2node", "ospf-p2p-3node"])
    assert all(row.metrics is not None for row in data.topologies)


def test_load_results_empty_dir_returns_empty_blob(tmp_path: Path) -> None:
    data = load_results(tmp_path)
    assert data.summary == {}
    assert data.topologies == []
    assert data.metrics == []


def test_load_results_topology_without_metrics(tmp_path: Path) -> None:
    # Smoke (vendor-only) run leaves <topology>.json but no diff/*/metrics.json.
    _write_run(tmp_path, "bgp-ibgp-2node", status="passed")
    _write_summary(tmp_path)
    data = load_results(tmp_path)
    row = next(r for r in data.topologies if r.topology == "bgp-ibgp-2node")
    assert row.metrics is None
    # Aggregate summary still loads.
    assert data.summary["topology_count"] == 2


def test_load_results_ignores_bench_summary_as_topology(
    results_dir_two_topologies: Path,
) -> None:
    # Regression: glob("*.json") sees bench_summary.json too, must skip.
    data = load_results(results_dir_two_topologies)
    names = {row.topology for row in data.topologies}
    assert "bench_summary" not in names


# ---- plots ---------------------------------------------------------------


def test_match_rate_bar_has_two_traces_sized_to_topologies(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    fig = match_rate_bar(data.metrics)
    assert len(fig.data) == 2  # batfish + hammerhead
    names = {trace.name for trace in fig.data}
    assert names == {"Batfish", "Hammerhead"}
    for trace in fig.data:
        assert len(trace.y) == len(data.metrics) == 2


def test_match_rate_bar_empty_metrics_returns_placeholder_figure() -> None:
    fig = match_rate_bar([])
    # Placeholder has an annotation instead of data traces.
    assert len(fig.data) == 0
    assert fig.layout.annotations and "no topologies" in fig.layout.annotations[0].text


def test_per_protocol_bar_aggregates_protocols_across_topologies(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    fig = per_protocol_bar(data.metrics)
    assert len(fig.data) == 2
    # Union of protocols across both topologies: bgp (1), connected (2), ospf (2).
    trace = fig.data[0]
    assert set(trace.x) == {"bgp", "ospf", "connected"}


def test_presence_bar_y_values_are_in_unit_interval(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    fig = presence_bar(data.metrics)
    for trace in fig.data:
        for y in trace.y:
            assert 0.0 <= y <= 1.0


# ---- markdown ------------------------------------------------------------


def test_markdown_contains_headline_table_with_mean_rates(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    md = render_markdown(data)
    assert "## Headline" in md
    # 88.0% from batfish_next_hop_match_rate_mean = 0.88.
    assert "88.0%" in md
    # 99.5% from hammerhead_next_hop_match_rate_mean = 0.995.
    assert "99.5%" in md


def test_markdown_per_topology_table_has_one_row_per_topology(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    md = render_markdown(data)
    # Row shape: "| <topology> | passed |"
    assert re.search(r"\|\s*bgp-ibgp-2node\s*\|\s*passed\s*\|", md)
    assert re.search(r"\|\s*ospf-p2p-3node\s*\|\s*passed\s*\|", md)


def test_markdown_surfaces_failed_topology_error(
    results_dir_with_failure: Path,
) -> None:
    data = load_results(results_dir_with_failure)
    md = render_markdown(data)
    assert "Failed + skipped" in md
    assert "route-reflector-6node" in md
    assert "batfish: timed out on deploy" in md
    # Notes are nested under the failure bullet.
    assert "convergence never achieved" in md


def test_markdown_empty_results_dir_still_renders(tmp_path: Path) -> None:
    data = load_results(tmp_path)
    md = render_markdown(data)
    # No bench_summary.json → placeholder message, but the header survives.
    assert "# Hammerhead Bench Report" in md
    assert "No ``bench_summary.json``" in md


def test_render_markdown_report_writes_file(
    results_dir_two_topologies: Path,
    tmp_path: Path,
) -> None:
    out = tmp_path / "sub" / "report.md"
    render_markdown_report(results_dir_two_topologies, out)
    assert out.exists()
    body = out.read_text()
    assert "# Hammerhead Bench Report" in body


# ---- html ----------------------------------------------------------------


def test_html_contains_plotly_bundle_once(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    body = render_html(data)
    # Plotly declares its version string in the inline JS; we should see
    # exactly one copy (otherwise the file is megabytes heavier than it
    # needs to be).
    #
    # Look for a stable Plotly internal marker — the namespace registration
    # line appears once per plotly.js bundle.
    bundle_markers = body.count("plotly.js")
    assert bundle_markers >= 1
    # The three chart divs each get a unique id; check they're distinct.
    chart_ids = re.findall(r'<div[^>]*id="([0-9a-f-]+)"[^>]*class="plotly-graph-div"', body)
    assert len(chart_ids) == 3
    assert len(set(chart_ids)) == 3


def test_html_headline_table_has_batfish_and_hammerhead_columns(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    body = render_html(data)
    assert "<h2>Headline</h2>" in body
    assert ">Batfish<" in body
    assert ">Hammerhead<" in body
    # Headline mean rates rendered as percentages.
    assert "88.0%" in body
    assert "99.5%" in body


def test_html_escapes_topology_names_and_errors(
    tmp_path: Path,
) -> None:
    # Write the run JSON with a filesystem-safe filename, then patch in an
    # angle-bracketed topology name so the loader carries the hostile value
    # through to the renderer. We want the escape test, not a filename test.
    _write_run(tmp_path, "evil-topology", status="failed", error="<img src=x onerror=alert(1)>")
    payload = json.loads((tmp_path / "evil-topology.json").read_text())
    payload["topology"] = "evil<script>alert(1)</script>"
    (tmp_path / "evil-topology.json").write_text(json.dumps(payload, indent=2) + "\n")
    _write_summary(tmp_path, failed=["evil<script>alert(1)</script>"])

    data = load_results(tmp_path)
    body = render_html(data)
    # Hostile topology name must never render as a live <script> tag.
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


def test_html_surfaces_failed_topology_notes(
    results_dir_with_failure: Path,
) -> None:
    data = load_results(results_dir_with_failure)
    body = render_html(data)
    assert "route-reflector-6node" in body
    assert "batfish: timed out on deploy" in body
    assert "convergence never achieved" in body


def test_html_methodology_and_hardware_sections_present(
    results_dir_two_topologies: Path,
) -> None:
    data = load_results(results_dir_two_topologies)
    body = render_html(data)
    assert "<h2>Methodology</h2>" in body
    assert "<h2>Hardware + software</h2>" in body
    # Python version line.
    assert "Python:" in body


def test_render_html_report_writes_file(
    results_dir_two_topologies: Path,
    tmp_path: Path,
) -> None:
    out = tmp_path / "sub" / "report.html"
    render_html_report(results_dir_two_topologies, out)
    assert out.exists()
    body = out.read_text()
    assert body.startswith("<!doctype html>")
    assert "</html>" in body
