"""Plotly figure factories — all figures are JSON-serializable and inline-safe.

Every factory returns a :class:`plotly.graph_objects.Figure` so
``harness.report.html`` can render them with
``plotly.io.to_html(fig, full_html=False, include_plotlyjs=False)`` and share
a single inlined Plotly bundle across the whole report.

The factories take a pre-loaded list of :class:`TopologyMetrics` (not a path)
so tests can exercise them without touching the filesystem. Empty inputs
return a Figure with an informative annotation rather than raising, so the
HTML report can render a "no-data" placeholder in place of the chart.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import plotly.graph_objects as go

from harness.diff.metrics import TopologyMetrics

__all__ = [
    "match_rate_bar",
    "per_protocol_bar",
    "presence_bar",
]


_BATFISH_COLOR = "#2b6cb0"  # blue-ish; Batfish's docs use a similar tone.
_HAMMERHEAD_COLOR = "#c53030"  # red-ish; matches our branding.


def match_rate_bar(metrics: Iterable[TopologyMetrics]) -> go.Figure:
    """Per-topology next-hop match-rate comparison (Batfish vs Hammerhead).

    X axis: topology name. Y axis: match rate in [0, 1]. Two bars per
    topology, one per simulator. The single most important chart in the
    report — answers "which tool is closer to vendor truth on each
    topology?"
    """
    metrics = list(metrics)
    fig = go.Figure()
    if not metrics:
        return _empty_fig("match_rate_bar: no topologies with diff metrics")

    topologies = [m.topology for m in metrics]
    fig.add_trace(
        go.Bar(
            name="Batfish",
            x=topologies,
            y=[m.batfish_next_hop_match_rate for m in metrics],
            marker_color=_BATFISH_COLOR,
            hovertemplate="%{x}<br>Batfish next-hop: %{y:.1%}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Hammerhead",
            x=topologies,
            y=[m.hammerhead_next_hop_match_rate for m in metrics],
            marker_color=_HAMMERHEAD_COLOR,
            hovertemplate="%{x}<br>Hammerhead next-hop: %{y:.1%}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Next-hop match rate vs vendor truth — per topology",
        barmode="group",
        yaxis={"range": [0.0, 1.0], "tickformat": ".0%", "title": "match rate"},
        xaxis={"title": "topology"},
        legend={"orientation": "h", "y": -0.2},
        margin={"l": 60, "r": 20, "t": 60, "b": 100},
    )
    return fig


def per_protocol_bar(metrics: Iterable[TopologyMetrics]) -> go.Figure:
    """Per-protocol next-hop match rate, averaged across all topologies.

    Answers "where do the tools disagree with vendor truth?" — OSPF, BGP,
    static, connected, etc. Rates are weighted means across topologies:
    a topology with more routes of a given protocol pulls that protocol's
    aggregate proportionally. Because we only have per-topology rates (not
    raw counts of matched-vs-total), we approximate the weighted mean with
    a plain mean. That's accurate when route counts are balanced; in
    unbalanced cases it understates the outlier topology. Good enough for
    a headline chart.
    """
    metrics = list(metrics)
    fig = go.Figure()
    if not metrics:
        return _empty_fig("per_protocol_bar: no topologies with diff metrics")

    batfish_by_proto: dict[str, list[float]] = defaultdict(list)
    hammerhead_by_proto: dict[str, list[float]] = defaultdict(list)
    for m in metrics:
        for proto, rate in m.batfish_per_protocol_next_hop_match_rate.items():
            batfish_by_proto[proto].append(rate)
        for proto, rate in m.hammerhead_per_protocol_next_hop_match_rate.items():
            hammerhead_by_proto[proto].append(rate)

    protocols = sorted(set(batfish_by_proto) | set(hammerhead_by_proto))
    if not protocols:
        return _empty_fig("per_protocol_bar: no protocols present in diff")

    fig.add_trace(
        go.Bar(
            name="Batfish",
            x=protocols,
            y=[_mean(batfish_by_proto.get(p, [])) for p in protocols],
            marker_color=_BATFISH_COLOR,
            hovertemplate="%{x}<br>Batfish: %{y:.1%}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Hammerhead",
            x=protocols,
            y=[_mean(hammerhead_by_proto.get(p, [])) for p in protocols],
            marker_color=_HAMMERHEAD_COLOR,
            hovertemplate="%{x}<br>Hammerhead: %{y:.1%}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Next-hop match rate — per protocol (mean across topologies)",
        barmode="group",
        yaxis={"range": [0.0, 1.0], "tickformat": ".0%", "title": "match rate"},
        xaxis={"title": "protocol"},
        legend={"orientation": "h", "y": -0.2},
        margin={"l": 60, "r": 20, "t": 60, "b": 80},
    )
    return fig


def presence_bar(metrics: Iterable[TopologyMetrics]) -> go.Figure:
    """Per-topology presence match rate — does the simulator carry the same
    set of (node, vrf, prefix) keys as vendor truth?

    Separate from next-hop match: a simulator can install the right next-hop
    on the wrong subset of routes. Presence-match = 1.0 means perfect
    coverage; next-hop-match = 1.0 means perfect correctness where there's
    coverage.
    """
    metrics = list(metrics)
    if not metrics:
        return _empty_fig("presence_bar: no topologies with diff metrics")

    topologies = [m.topology for m in metrics]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="Batfish",
            x=topologies,
            y=[m.batfish_presence_match_rate for m in metrics],
            marker_color=_BATFISH_COLOR,
            hovertemplate="%{x}<br>Batfish presence: %{y:.1%}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Hammerhead",
            x=topologies,
            y=[m.hammerhead_presence_match_rate for m in metrics],
            marker_color=_HAMMERHEAD_COLOR,
            hovertemplate="%{x}<br>Hammerhead presence: %{y:.1%}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Presence match rate — per topology",
        barmode="group",
        yaxis={"range": [0.0, 1.0], "tickformat": ".0%", "title": "presence match"},
        xaxis={"title": "topology"},
        legend={"orientation": "h", "y": -0.2},
        margin={"l": 60, "r": 20, "t": 60, "b": 100},
    )
    return fig


def _empty_fig(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
        font={"size": 14, "color": "#888"},
    )
    fig.update_layout(
        xaxis={"visible": False},
        yaxis={"visible": False},
        margin={"l": 20, "r": 20, "t": 20, "b": 20},
    )
    return fig


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
