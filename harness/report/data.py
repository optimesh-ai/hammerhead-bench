"""Shared results-directory loader.

Reads the four file shapes produced by :mod:`harness.pipeline` + :mod:`harness.cli`
and returns a single :class:`ReportData` blob the Markdown + HTML renderers can
consume without re-walking the filesystem.

Layout (matches :func:`harness.pipeline.run_topology` + :func:`harness.cli._write_run_result`):

- ``<results_dir>/<topology>.json``            — :class:`TopologyRunResult` summary
- ``<results_dir>/diff/<topology>/metrics.json`` — :class:`TopologyMetrics`
- ``<results_dir>/bench_summary.json``         — aggregate mean across topologies

Missing files degrade gracefully: a topology without a metrics.json is listed
as "no-diff" in the report but the aggregate still renders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.diff.metrics import TopologyMetrics

__all__ = ["ReportData", "TopologyRow", "load_results"]


@dataclass(slots=True)
class TopologyRow:
    """One row per topology in the report tables.

    ``metrics`` is absent when the diff phase didn't run (vendor-only smoke,
    a simulator crashed, etc.). ``run`` is the raw dict from
    ``<results_dir>/<topology>.json`` so the report can surface status +
    error + notes verbatim.
    """

    topology: str
    run: dict[str, Any]
    metrics: TopologyMetrics | None = None


@dataclass(slots=True)
class ReportData:
    """Everything the report needs in one structured blob."""

    results_dir: Path
    summary: dict[str, Any] = field(default_factory=dict)
    topologies: list[TopologyRow] = field(default_factory=list)

    @property
    def metrics(self) -> list[TopologyMetrics]:
        """Just the topologies that produced metrics (diff ran)."""
        return [row.metrics for row in self.topologies if row.metrics is not None]


# The per-topology run JSON is a flat dict. The aggregate summary too. No
# separate schema class — these are display-only.


def load_results(results_dir: Path) -> ReportData:
    """Walk a results directory and load every artifact the report needs.

    ``results_dir`` is the same directory passed to ``hammerhead-bench bench``.
    An empty or non-existent directory returns a :class:`ReportData` with
    ``summary = {}`` and ``topologies = []`` — the Markdown + HTML renderers
    emit a "no results" stub in that case rather than crashing.
    """
    summary_path = results_dir / "bench_summary.json"
    summary: dict[str, Any] = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())

    topologies: list[TopologyRow] = []
    if results_dir.is_dir():
        # Per-topology run summaries live as top-level `<topology>.json`.
        # `bench_summary.json` lives at the same level, skip it.
        for path in sorted(results_dir.glob("*.json")):
            if path.name == "bench_summary.json":
                continue
            run = json.loads(path.read_text())
            topology = run.get("topology") or path.stem
            metrics = _load_metrics(results_dir / "diff" / topology / "metrics.json")
            topologies.append(TopologyRow(topology=topology, run=run, metrics=metrics))

    return ReportData(results_dir=results_dir, summary=summary, topologies=topologies)


def _load_metrics(path: Path) -> TopologyMetrics | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # TopologyMetrics is a plain dataclass — reconstruct via keyword-args so
    # added/removed fields surface as a loud TypeError rather than silently
    # losing data.
    return TopologyMetrics(**data)
