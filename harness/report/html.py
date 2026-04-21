"""Static HTML report — Phase 9 deliverable.

Self-contained single page: Plotly bundle inlined once at the top, three
figures (per-topology next-hop, per-protocol, per-topology presence),
headline table, per-topology table, per-protocol table, failure list,
hardware + methodology disclosures, and a link to the raw JSON.

No CDN, no external assets. The resulting file opens cleanly from a
``file://`` URL with JavaScript enabled. File size is dominated by the
Plotly JS bundle (~4 MB uncompressed); that's the tradeoff for offline
portability.
"""

from __future__ import annotations

import html
import platform
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly.io as pio
from plotly import graph_objects as go
from plotly.offline import get_plotlyjs

from harness.diff.metrics import TopologyMetrics
from harness.report.data import ReportData, TopologyRow, load_results
from harness.report.plots import match_rate_bar, per_protocol_bar, presence_bar

__all__ = ["render_html", "render_html_report"]


_CSS = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif; margin: 2rem auto; max-width: 1100px;
        color: #1a202c; line-height: 1.55; padding: 0 1rem; }
    h1 { border-bottom: 2px solid #e2e8f0; padding-bottom: 0.3em; }
    h2 { margin-top: 2.5rem; border-bottom: 1px solid #edf2f7; padding-bottom: 0.2em; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }
    th, td { border: 1px solid #e2e8f0; padding: 0.45rem 0.7rem; text-align: left; }
    th { background: #f7fafc; font-weight: 600; }
    td.rate, th.rate { text-align: right; font-variant-numeric: tabular-nums; }
    td.fail { color: #c53030; font-weight: 600; }
    td.pass { color: #2f855a; font-weight: 600; }
    .chart { margin: 1.5rem 0 2.5rem; }
    .meta { color: #4a5568; font-size: 0.92em; }
    code { background: #f7fafc; padding: 0.1em 0.35em; border-radius: 3px; }
    .methodology li { margin-bottom: 0.4rem; }
    .fail-list li { margin-bottom: 0.3rem; }
"""


def render_html_report(results_dir: Path, out_path: Path) -> Path:
    """Load ``results_dir`` and write an HTML report to ``out_path``.

    Returns the final ``out_path`` so callers can log / display it.
    """
    data = load_results(results_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(data))
    return out_path


def render_html(data: ReportData) -> str:
    """Render the HTML body from a :class:`ReportData` blob."""
    generated_iso = datetime.now(tz=UTC).isoformat(timespec="seconds")
    sections: list[str] = []
    sections.append("<h1>Hammerhead Bench Report</h1>")
    sections.append(
        f'<p class="meta">Generated <code>{html.escape(generated_iso)}</code> from '
        f"<code>{html.escape(str(data.results_dir))}</code>.</p>"
    )
    sections.append(_headline_section(data.summary))
    sections.append(
        _chart_section(
            "Next-hop match rate — per topology",
            match_rate_bar(data.metrics),
            anchor="fig-nh",
        )
    )
    sections.append(
        _chart_section(
            "Next-hop match rate — per protocol",
            per_protocol_bar(data.metrics),
            anchor="fig-proto",
        )
    )
    sections.append(
        _chart_section(
            "Presence match rate — per topology",
            presence_bar(data.metrics),
            anchor="fig-presence",
        )
    )
    sections.append(_per_topology_section(data.topologies))
    sections.append(_per_protocol_section(data.metrics))
    sections.append(_failed_section(data.topologies))
    sections.append(_methodology_section())
    sections.append(_hardware_section())

    # Plotly's JS bundle is big but it's the price of "no CDN". Injected
    # once in a <script> block so all three figures share one copy.
    plotly_js = get_plotlyjs()

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hammerhead Bench Report</title>
<style>{_CSS}</style>
<script type="text/javascript">{plotly_js}</script>
</head>
<body>
{"".join(sections)}
</body>
</html>
"""


# ---- sections ------------------------------------------------------------


def _headline_section(summary: dict[str, Any]) -> str:
    if not summary:
        return (
            "<h2>Headline</h2>"
            "<p><em>No <code>bench_summary.json</code> — run "
            "<code>hammerhead-bench bench</code> first.</em></p>"
        )
    n = summary.get("topology_count", 0)
    failed = summary.get("failed_topologies") or []
    rows: list[str] = []
    for label, bkey, hkey in [
        (
            "Presence match rate",
            "batfish_presence_match_rate_mean",
            "hammerhead_presence_match_rate_mean",
        ),
        (
            "Next-hop match rate",
            "batfish_next_hop_match_rate_mean",
            "hammerhead_next_hop_match_rate_mean",
        ),
        (
            "Protocol match rate",
            "batfish_protocol_match_rate_mean",
            "hammerhead_protocol_match_rate_mean",
        ),
        (
            "BGP attribute match rate",
            "batfish_bgp_attr_match_rate_mean",
            "hammerhead_bgp_attr_match_rate_mean",
        ),
    ]:
        rows.append(
            f"<tr><td>{html.escape(label)}</td>"
            f'<td class="rate">{_fmt_rate(summary.get(bkey))}</td>'
            f'<td class="rate">{_fmt_rate(summary.get(hkey))}</td></tr>'
        )
    meta = [f"Topologies: <strong>{int(n)}</strong>"]
    if failed:
        meta.append(f"Failed: <strong>{len(failed)}</strong> ({html.escape(', '.join(failed))})")
    return (
        "<h2>Headline</h2>"
        f'<p class="meta">{" &middot; ".join(meta)}</p>'
        "<table>"
        "<thead><tr><th>Metric</th>"
        '<th class="rate">Batfish</th><th class="rate">Hammerhead</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _chart_section(title: str, fig: go.Figure, *, anchor: str) -> str:
    # include_plotlyjs=False because the bundle is injected once in <head>.
    # full_html=False so we can nest in an arbitrary surrounding layout.
    body = pio.to_html(fig, full_html=False, include_plotlyjs=False)
    return (
        f'<h2 id="{html.escape(anchor)}">{html.escape(title)}</h2><div class="chart">{body}</div>'
    )


def _per_topology_section(topologies: list[TopologyRow]) -> str:
    if not topologies:
        return "<h2>Per-topology</h2><p><em>No topologies found in the results directory.</em></p>"
    header = (
        "<thead><tr>"
        "<th>Topology</th><th>Status</th>"
        '<th class="rate">Batfish NH</th><th class="rate">Hammerhead NH</th>'
        '<th class="rate">Batfish proto</th><th class="rate">Hammerhead proto</th>'
        '<th class="rate">Batfish BGP</th><th class="rate">Hammerhead BGP</th>'
        "</tr></thead>"
    )
    rows: list[str] = []
    for row in topologies:
        status = row.run.get("status", "?")
        status_cls = "pass" if status == "passed" else "fail" if status == "failed" else ""
        if row.metrics is None:
            cells = ["-"] * 6
        else:
            m = row.metrics
            cells = [
                _fmt_rate(m.batfish_next_hop_match_rate),
                _fmt_rate(m.hammerhead_next_hop_match_rate),
                _fmt_rate(m.batfish_protocol_match_rate),
                _fmt_rate(m.hammerhead_protocol_match_rate),
                _fmt_rate(m.batfish_bgp_attr_match_rate),
                _fmt_rate(m.hammerhead_bgp_attr_match_rate),
            ]
        rate_cells = "".join(f'<td class="rate">{c}</td>' for c in cells)
        rows.append(
            f"<tr><td>{html.escape(row.topology)}</td>"
            f'<td class="{status_cls}">{html.escape(status)}</td>'
            f"{rate_cells}</tr>"
        )
    return f"<h2>Per-topology</h2><table>{header}<tbody>{''.join(rows)}</tbody></table>"


def _per_protocol_section(metrics: Iterable[TopologyMetrics]) -> str:
    metrics = list(metrics)
    if not metrics:
        return (
            "<h2>Per-protocol next-hop match</h2>"
            "<p><em>No per-topology metrics — breakdown unavailable.</em></p>"
        )
    batfish_by_proto: dict[str, list[float]] = defaultdict(list)
    hammerhead_by_proto: dict[str, list[float]] = defaultdict(list)
    for m in metrics:
        for proto, rate in m.batfish_per_protocol_next_hop_match_rate.items():
            batfish_by_proto[proto].append(rate)
        for proto, rate in m.hammerhead_per_protocol_next_hop_match_rate.items():
            hammerhead_by_proto[proto].append(rate)
    protocols = sorted(set(batfish_by_proto) | set(hammerhead_by_proto))
    if not protocols:
        return (
            "<h2>Per-protocol next-hop match</h2><p><em>No protocols present in any diff.</em></p>"
        )
    rows: list[str] = []
    for proto in protocols:
        b = batfish_by_proto.get(proto, [])
        h = hammerhead_by_proto.get(proto, [])
        rows.append(
            f"<tr><td>{html.escape(proto)}</td>"
            f'<td class="rate">{_fmt_rate(_mean(b)) if b else "-"}</td>'
            f'<td class="rate">{_fmt_rate(_mean(h)) if h else "-"}</td></tr>'
        )
    return (
        "<h2>Per-protocol next-hop match</h2>"
        "<table>"
        "<thead><tr><th>Protocol</th>"
        '<th class="rate">Batfish (mean)</th><th class="rate">Hammerhead (mean)</th>'
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _failed_section(topologies: list[TopologyRow]) -> str:
    failed = [row for row in topologies if row.run.get("status") != "passed"]
    if not failed:
        return "<h2>Failed + skipped</h2><p><em>All selected topologies passed.</em></p>"
    items: list[str] = []
    for row in failed:
        status = row.run.get("status", "?")
        err = row.run.get("error")
        desc = html.escape(status)
        if err:
            desc += f": {html.escape(err)}"
        notes = row.run.get("notes") or []
        sub = ""
        if notes:
            sub = "<ul>" + "".join(f"<li>{html.escape(str(n))}</li>" for n in notes) + "</ul>"
        items.append(f"<li><strong>{html.escape(row.topology)}</strong> — {desc}{sub}</li>")
    return f'<h2>Failed + skipped</h2><ul class="fail-list">{"".join(items)}</ul>'


def _methodology_section() -> str:
    return (
        "<h2>Methodology</h2>"
        '<ul class="methodology">'
        "<li>Every topology is rendered to containerlab configs, deployed as a "
        "real Docker lab, and given time to converge. Vendor truth is the FIB "
        "each running container reports (<code>vtysh</code> for FRR, "
        "<code>Cli | json</code> for cEOS).</li>"
        "<li>The same rendered configs are fed to Batfish (via pybatfish) and "
        "Hammerhead (via the <code>hammerhead</code> CLI) out-of-band. Both "
        "simulators produce per-(node, vrf) FIB JSON in the same schema.</li>"
        "<li>The diff engine canonicalizes all three sources (next-hop order, "
        "ECMP order, default VRF naming) before comparing. A simulator's "
        "match rate is the fraction of vendor-present (node, vrf, prefix) keys "
        "for which the simulator carries the same next-hop set, protocol, and "
        "(for BGP) AS_PATH + LOCAL_PREF + MED.</li>"
        "<li>Rates are per-simulator. We never compare Batfish directly to "
        "Hammerhead — the yardstick is always vendor truth.</li>"
        "<li>Divide-by-zero on a metric returns 1.0 (an empty set trivially "
        "matches itself). Zero-route topologies are disclosed via the "
        "<code>total_routes_*</code> fields in the raw metrics JSON.</li>"
        "</ul>"
    )


def _hardware_section() -> str:
    uname = platform.uname()
    return (
        "<h2>Hardware + software</h2>"
        '<ul class="meta">'
        f"<li>System: <code>{html.escape(uname.system)}</code> "
        f"<code>{html.escape(uname.release)}</code> "
        f"<code>{html.escape(uname.machine)}</code></li>"
        f"<li>Python: <code>{html.escape(platform.python_version())}</code></li>"
        f"<li>Processor: <code>{html.escape(uname.processor or 'unknown')}</code></li>"
        "</ul>"
    )


# ---- helpers -------------------------------------------------------------


def _fmt_rate(r: float | None) -> str:
    if r is None:
        return "-"
    return f"{r * 100:.1f}%"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
