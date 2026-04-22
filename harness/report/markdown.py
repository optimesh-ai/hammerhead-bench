"""Markdown report — thin, CI-digest friendly, no figures.

Consumed by the ``hammerhead-bench report`` CLI subcommand. Shape:

1. Headline block — mean match rates across every topology (Batfish +
   Hammerhead side by side).
2. Per-topology table — one row per topology, status + per-simulator
   rates. Failed/skipped rows surface ``status`` + ``error`` from the
   per-topology run JSON so a CI reader sees the cause in-line.
3. Per-protocol table — mean per-protocol next-hop match rate across the
   run.
4. Raw-data pointer — relative link to the JSON artifacts.

Everything is pure-Python string building; no templating engine required.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from harness.diff.metrics import TopologyMetrics
from harness.report.data import ReportData, TopologyRow, load_results

__all__ = ["render_markdown", "render_markdown_report"]


def render_markdown_report(results_dir: Path, out_path: Path) -> Path:
    """Load ``results_dir`` and write a Markdown report to ``out_path``.

    Returns the final ``out_path`` so callers can log / display it.
    Creates the parent directory if needed.
    """
    data = load_results(results_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(data))
    return out_path


def render_markdown(data: ReportData) -> str:
    """Render the Markdown body from a :class:`ReportData` blob.

    Kept as a pure function so tests can build a ReportData in-memory
    without writing to disk. The renderer branches on
    ``summary["mode"]`` — ``"sim_only"`` gets the Batfish-vs-Hammerhead
    agreement shape; anything else (or missing) gets the with-truth
    three-way match shape.
    """
    lines: list[str] = []
    lines.append("# Hammerhead Bench Report")
    lines.append("")
    lines.append(f"Results: `{data.results_dir}`")
    lines.append("")

    if data.summary.get("mode") == "sim_only":
        lines.extend(_sim_only_headline_block(data.summary))
        lines.append("")
        lines.extend(_sim_only_per_topology_table(data.topologies))
    else:
        lines.extend(_headline_block(data.summary))
        lines.append("")
        lines.extend(_per_topology_table(data.topologies))
        lines.append("")
        lines.extend(_per_protocol_table(data.metrics))
    lines.append("")
    lines.extend(_failed_block(data.topologies))
    lines.append("")
    lines.append("## Raw data")
    lines.append("")
    lines.append("- `bench_summary.json` — aggregate mean across topologies")
    lines.append("- `<topology>.json` — per-topology run result")
    if data.summary.get("mode") == "sim_only":
        lines.append("- `diff_sim_only/<topology>/agreement.json` — per-topology B↔H agreement")
        lines.append("- `diff_sim_only/<topology>/records.json` — one row per (node, vrf, prefix)")
    else:
        lines.append("- `diff/<topology>/metrics.json` — per-topology headline rates")
        lines.append("- `diff/<topology>/records.json` — one row per (node, vrf, prefix)")
        lines.append("- `vendor_truth/<topology>/<node>__<vrf>.json` — ground truth FIB")
    lines.append("- `batfish/<topology>/<node>__<vrf>.json` — Batfish FIB")
    lines.append("- `hammerhead/<topology>/<node>__<vrf>.json` — Hammerhead FIB")
    lines.append("")
    return "\n".join(lines)


# ---- sections ------------------------------------------------------------


def _headline_block(summary: dict[str, Any]) -> list[str]:
    lines = ["## Headline"]
    if not summary:
        lines.append("")
        lines.append("_No ``bench_summary.json`` — did ``hammerhead-bench bench`` run?_")
        return lines
    n = summary.get("topology_count", 0)
    lines.append("")
    lines.append(f"Topologies: **{n}**")
    failed = summary.get("failed_topologies") or []
    if failed:
        lines.append(f"Failed topologies: **{len(failed)}** — {', '.join(failed)}")
    lines.append("")
    lines.append("| Metric | Batfish | Hammerhead |")
    lines.append("|---|---:|---:|")
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
        lines.append(
            f"| {label} | {_fmt_rate(summary.get(bkey))} | {_fmt_rate(summary.get(hkey))} |"
        )
    return lines


def _sim_only_headline_block(summary: dict[str, Any]) -> list[str]:
    """Batfish↔Hammerhead agreement headline, with honest Jaccard coverage."""
    lines = ["## Headline (sim-only — Hammerhead vs Batfish)"]
    if not summary:
        lines.append("")
        lines.append("_No ``bench_summary.json``._")
        return lines
    n = summary.get("topology_count", 0)
    covered = summary.get("covered_topology_count", 0)
    lines.append("")
    lines.append(f"Topologies: **{n}** · with non-empty intersection: **{covered}**")
    lines.append("")
    lines.append("Agreement is computed over `(node, vrf, prefix)` cells "
                 "carried by **both** simulators. The two means below surface "
                 "the same quantity with and without vacuous-truth topologies "
                 "(zero intersection ⇒ counted as 1.0 in the naive mean).")
    lines.append("")
    failed = summary.get("failed_topologies") or []
    if failed:
        lines.append(f"Failed topologies: **{len(failed)}** — {', '.join(failed)}")
        lines.append("")
    mean_cov = summary.get("mean_coverage")
    lines.append("| Metric | Naive (all topologies) | Covered only |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| Next-hop agreement | "
        f"{_fmt_rate(summary.get('next_hop_agreement_mean'))} | "
        f"{_fmt_rate(summary.get('next_hop_agreement_mean_covered'))} |"
    )
    lines.append(
        f"| Protocol agreement | "
        f"{_fmt_rate(summary.get('protocol_agreement_mean'))} | "
        f"{_fmt_rate(summary.get('protocol_agreement_mean_covered'))} |"
    )
    lines.append(
        f"| BGP attribute agreement | "
        f"{_fmt_rate(summary.get('bgp_attr_agreement_mean'))} | "
        f"{_fmt_rate(summary.get('bgp_attr_agreement_mean_covered'))} |"
    )
    lines.append(f"| Mean coverage `|B∩H|/|B∪H|` | {_fmt_rate(mean_cov)} | — |")
    lines.append("")
    bf_wall = summary.get("total_batfish_wall_s", 0.0) or 0.0
    hh_wall = summary.get("total_hammerhead_wall_s", 0.0) or 0.0
    bf_routes = summary.get("total_batfish_routes", 0)
    hh_routes = summary.get("total_hammerhead_routes", 0)
    speedup = (bf_wall / hh_wall) if hh_wall > 0 else None
    lines.append("| Totals | Batfish | Hammerhead |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Routes produced | {bf_routes} | {hh_routes} |")
    lines.append(f"| Wall time (s) | {bf_wall:.2f} | {hh_wall:.2f} |")
    if speedup is not None:
        lines.append(f"| Hammerhead speedup | — | **{speedup:.1f}×** |")
    return lines


def _sim_only_per_topology_table(topologies: list[TopologyRow]) -> list[str]:
    """Per-topology agreement table for sim-only mode.

    Reads the ``agreement`` dict directly from each run — the with-truth
    ``metrics`` field is always ``None`` in sim-only mode.
    """
    lines = ["## Per-topology"]
    if not topologies:
        lines.append("")
        lines.append("_No topologies found in the results directory._")
        return lines
    lines.append("")
    lines.append(
        "| Topology | Status | B routes | H routes | "
        "`|∩|` | `|∪|` | coverage | "
        "next-hop | proto | BGP attr | B wall (s) | H wall (s) |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in topologies:
        status = row.run.get("status", "?")
        agreement = row.run.get("agreement") or {}
        if not agreement:
            lines.append(
                f"| {row.topology} | {status} | - | - | - | - | - | - | - | - | - | - |"
            )
            continue
        lines.append(
            f"| {row.topology} | {status} | "
            f"{agreement.get('batfish_routes', '-')} | "
            f"{agreement.get('hammerhead_routes', '-')} | "
            f"{agreement.get('both_sides_keys', '-')} | "
            f"{agreement.get('union_keys', '-')} | "
            f"{_fmt_rate(agreement.get('coverage'))} | "
            f"{_fmt_rate(agreement.get('next_hop_agreement'))} | "
            f"{_fmt_rate(agreement.get('protocol_agreement'))} | "
            f"{_fmt_rate(agreement.get('bgp_attr_agreement'))} | "
            f"{_fmt_wall(agreement.get('batfish_wall_s'))} | "
            f"{_fmt_wall(agreement.get('hammerhead_wall_s'))} |"
        )
    return lines


def _per_topology_table(topologies: list[TopologyRow]) -> list[str]:
    lines = ["## Per-topology"]
    if not topologies:
        lines.append("")
        lines.append("_No topologies found in the results directory._")
        return lines
    lines.append("")
    lines.append(
        "| Topology | Status | "
        "Batfish NH | Hammerhead NH | "
        "Batfish proto | Hammerhead proto | "
        "Batfish BGP | Hammerhead BGP |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in topologies:
        status = row.run.get("status", "?")
        if row.metrics is None:
            lines.append(f"| {row.topology} | {status} | - | - | - | - | - | - |")
            continue
        m = row.metrics
        lines.append(
            f"| {row.topology} | {status} | "
            f"{_fmt_rate(m.batfish_next_hop_match_rate)} | "
            f"{_fmt_rate(m.hammerhead_next_hop_match_rate)} | "
            f"{_fmt_rate(m.batfish_protocol_match_rate)} | "
            f"{_fmt_rate(m.hammerhead_protocol_match_rate)} | "
            f"{_fmt_rate(m.batfish_bgp_attr_match_rate)} | "
            f"{_fmt_rate(m.hammerhead_bgp_attr_match_rate)} |"
        )
    return lines


def _per_protocol_table(metrics: Iterable[TopologyMetrics]) -> list[str]:
    metrics = list(metrics)
    lines = ["## Per-protocol next-hop match"]
    if not metrics:
        lines.append("")
        lines.append("_No per-topology metrics — per-protocol breakdown unavailable._")
        return lines
    batfish_by_proto: dict[str, list[float]] = defaultdict(list)
    hammerhead_by_proto: dict[str, list[float]] = defaultdict(list)
    for m in metrics:
        for proto, rate in m.batfish_per_protocol_next_hop_match_rate.items():
            batfish_by_proto[proto].append(rate)
        for proto, rate in m.hammerhead_per_protocol_next_hop_match_rate.items():
            hammerhead_by_proto[proto].append(rate)
    protocols = sorted(set(batfish_by_proto) | set(hammerhead_by_proto))
    if not protocols:
        lines.append("")
        lines.append("_No protocols present in any diff — was any topology canonicalized?_")
        return lines
    lines.append("")
    lines.append("| Protocol | Batfish (mean) | Hammerhead (mean) |")
    lines.append("|---|---:|---:|")
    for proto in protocols:
        b = batfish_by_proto.get(proto, [])
        h = hammerhead_by_proto.get(proto, [])
        lines.append(
            f"| {proto} | "
            f"{_fmt_rate(_mean(b)) if b else '-'} | "
            f"{_fmt_rate(_mean(h)) if h else '-'} |"
        )
    return lines


def _failed_block(topologies: list[TopologyRow]) -> list[str]:
    failed = [row for row in topologies if row.run.get("status") != "passed"]
    lines = ["## Failed + skipped"]
    if not failed:
        lines.append("")
        lines.append("_All selected topologies passed._")
        return lines
    lines.append("")
    for row in failed:
        status = row.run.get("status", "?")
        err = row.run.get("error")
        lines.append(f"- **{row.topology}** — {status}" + (f": {err}" if err else ""))
        for note in row.run.get("notes") or []:
            lines.append(f"    - {note}")
    return lines


# ---- helpers -------------------------------------------------------------


def _fmt_rate(r: float | None) -> str:
    if r is None:
        return "-"
    return f"{r * 100:.1f}%"


def _fmt_wall(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s:.2f}"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
