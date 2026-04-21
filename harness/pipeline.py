"""Per-topology orchestration. Phase 2+ implements; phase 1 defines the shape.

Sequential pipeline, top to bottom, one topology at a time:

    render -> deploy -> converge -> extract(vendor) -> teardown -> verify
    -> run(batfish) -> run(hammerhead) -> diff -> write_results
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TopologyRun:
    """Pointer to one topology directory; the pipeline consumes instances of this."""

    name: str
    path: Path
    node_count: int


def run_topology(_topo: TopologyRun) -> None:  # pragma: no cover - phase 2+
    raise NotImplementedError("pipeline.run_topology: phase 2+ deliverable")
