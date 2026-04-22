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
    elif data.summary.get("mode") == "frr_only_truth":
        lines.extend(_sim_only_headline_block(data.summary))
        lines.append("")
        lines.extend(_sim_only_per_topology_table(data.topologies))
        truth_section = _truth_section(data.topologies)
        if truth_section:
            lines.append("")
            lines.extend(truth_section)
    else:
        lines.extend(_headline_block(data.summary))
        lines.append("")
        lines.extend(_per_topology_table(data.topologies))
        lines.append("")
        lines.extend(_per_protocol_table(data.metrics))
        truth_section = _truth_section(data.topologies)
        if truth_section:
            lines.append("")
            lines.extend(truth_section)
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
    ``metrics`` field is always ``None`` in sim-only mode. When trials > 1,
    every wall-clock column renders as ``mean ± std`` using the
    per-topology ``agreement.trial_stats`` payload.

    Columns (README § 1 canonical order):

    - ``Nodes`` — ``agreement.nodes`` (``len(spec.nodes)``).
    - ``Routes (bf / hh)`` — ``batfish_routes`` / ``hammerhead_routes``.
    - ``Presence`` — ``|B ∩ H| / |B ∪ H|`` (Jaccard).
    - ``NH agree`` — ``next_hop_agreement``.
    - ``BF wall`` — ``batfish_wall_s`` (JVM start + upload + solve).
    - ``HH wall`` — ``hammerhead_wall_s`` (simulate + rib + JSON + write).
    - ``Wall ratio`` — ``batfish_wall_s / hammerhead_wall_s`` (conservative
      upper bound; JVM-dominated at small scale).
    - ``Fair ratio`` — ``batfish_simulate_s / (hammerhead_simulate_s +
      hammerhead_rib_total_s)`` (apples-to-apples; the one we recommend
      citing, formal definition in README § 2).

    The legacy "solve ratio" column is deliberately dropped: it paired
    Batfish's solve-plus-materialize against Hammerhead's solve-only and
    so structurally flattered Hammerhead. The asymmetric ratio is still
    in the JSON under ``agreement.asym_ratio`` with an in-band
    ``asym_ratio_note`` caveat, but it does not surface here.
    """
    lines = ["## Per-topology"]
    if not topologies:
        lines.append("")
        lines.append("_No topologies found in the results directory._")
        return lines
    lines.append("")
    lines.append(
        "| Topology | Status | Nodes | Routes (bf / hh) | "
        "Presence | NH agree | BF wall (s) | HH wall (s) | "
        "Wall ratio | Fair ratio |"
    )
    lines.append(
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for row in topologies:
        status = row.run.get("status", "?")
        agreement = row.run.get("agreement") or {}
        if not agreement:
            lines.append(
                f"| {row.topology} | {status} | "
                "- | - | - | - | - | - | - | - |"
            )
            continue
        trial_stats = agreement.get("trial_stats") or {}
        bf_wall_cell = _fmt_wall_with_std(
            agreement.get("batfish_wall_s"),
            trial_stats.get("batfish_wall_s"),
        )
        hh_wall_cell = _fmt_wall_with_std(
            agreement.get("hammerhead_wall_s"),
            trial_stats.get("hammerhead_wall_s"),
        )
        presence_val = agreement.get("presence")
        if presence_val is None:
            presence_val = agreement.get("coverage")
        wall_ratio = agreement.get("wall_ratio")
        if wall_ratio is None:
            bw = agreement.get("batfish_wall_s")
            hw = agreement.get("hammerhead_wall_s")
            if bw is not None and hw is not None and hw > 0:
                wall_ratio = bw / hw
        fair_ratio = agreement.get("fair_ratio")
        if fair_ratio is None:
            fair_ratio = agreement.get("solve_plus_materialize_ratio")
        if fair_ratio is None:
            # Last-resort back-fill for legacy sidecars that only carry
            # simulate_s (no rib_total_s) — equivalent to the old asym
            # ratio, but only as a back-compat fallback.
            bs = agreement.get("batfish_simulate_s")
            hs = agreement.get("hammerhead_simulate_s")
            if bs is not None and hs is not None and hs > 0:
                fair_ratio = bs / hs
        nodes = agreement.get("nodes")
        routes_cell = (
            f"{agreement.get('batfish_routes', '-')} / "
            f"{agreement.get('hammerhead_routes', '-')}"
        )
        lines.append(
            f"| {row.topology} | {status} | "
            f"{nodes if nodes is not None else '-'} | "
            f"{routes_cell} | "
            f"{_fmt_rate(presence_val)} | "
            f"{_fmt_rate(agreement.get('next_hop_agreement'))} | "
            f"{bf_wall_cell} | "
            f"{hh_wall_cell} | "
            f"{_fmt_solve_ratio(wall_ratio)} | "
            f"{_fmt_solve_ratio(fair_ratio)} |"
        )
    lines.append("")
    lines.append(
        "Wall ratio includes JVM startup and snapshot upload on the "
        "Batfish side; fair ratio is the apples-to-apples "
        "solve+materialize comparison defined in README § 2."
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


def _truth_section(topologies: list[TopologyRow]) -> list[str]:
    """Ground-truth agreement (FRR subset) — omitted entirely when empty.

    Only rendered when at least one topology carries
    ``truth_source != None``. Columns expose all three pairwise agreement
    triads (B↔T, H↔T, B↔H) plus the truth-route count so reviewers can
    see the denominator alongside the rate.
    """
    rows_with_truth = [
        row for row in topologies
        if (row.run.get("truth_source") is not None)
        and (row.run.get("three_way_agreement") is not None)
    ]
    if not rows_with_truth:
        return []

    lines: list[str] = []
    lines.append("## Ground-truth agreement (FRR subset)")
    lines.append("")
    lines.append(
        "Collected on Linux hosts with containerlab + Docker. The subset of "
        "topologies that are pure FRR/Cumulus with ≤20 nodes can additionally "
        "be compared against live vendor RIBs (T). Topologies outside the "
        "subset fall back to the sim-only table above; their ``truth_source`` "
        "is null in the result JSON."
    )
    lines.append("")
    lines.append(
        "| Topology | Truth routes | B vs T presence | B vs T NH | "
        "H vs T presence | H vs T NH | B vs H presence | B vs H NH |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows_with_truth:
        a = row.run.get("three_way_agreement") or {}
        lines.append(
            f"| {row.topology} | "
            f"{a.get('truth_routes', '-')} | "
            f"{_fmt_rate(a.get('batfish_vs_truth_presence'))} | "
            f"{_fmt_rate(a.get('batfish_vs_truth_next_hop'))} | "
            f"{_fmt_rate(a.get('hammerhead_vs_truth_presence'))} | "
            f"{_fmt_rate(a.get('hammerhead_vs_truth_next_hop'))} | "
            f"{_fmt_rate(a.get('batfish_vs_hammerhead_presence'))} | "
            f"{_fmt_rate(a.get('batfish_vs_hammerhead_next_hop'))} |"
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


def _fmt_solve_ratio(ratio: float | None) -> str:
    """Render ``batfish_simulate_s / hammerhead_simulate_s`` as ``N.N×``.

    None or non-positive ratios render as ``-`` so the column reader can
    spot "Batfish solve stat missing / Hammerhead solve time zero" rows
    without decoding a float.
    """
    if ratio is None or ratio <= 0:
        return "-"
    return f"{ratio:.1f}\u00d7"


def _fmt_wall_with_std(scalar: float | None, stats: dict | None) -> str:
    """Render a wall-clock cell, preferring ``mean ± std`` when trials ran.

    ``stats`` is the per-topology ``agreement.trial_stats[<field>]`` payload
    (``{"mean": ..., "std": ..., "min": ..., "max": ...}``) produced when
    ``--trials N`` runs N >= 2 trials. Falls back to the scalar mean field
    for N == 1 runs (existing single-trial shape).
    """
    if stats is not None:
        return f"{stats.get('mean', 0.0):.2f} ± {stats.get('std', 0.0):.2f}"
    if scalar is None:
        return "-"
    return f"{scalar:.2f}"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
