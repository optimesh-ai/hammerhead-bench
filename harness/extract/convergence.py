"""Convergence detection. Phase 2+ implements.

Contract (spec): a node is converged when
  - all configured BGP sessions are Established, AND
  - the route count is identical across two consecutive 15 s samples.

Hard cap: 5 min wall-clock per node. Failure to converge aborts the topology
and marks it failed in the final report. NEVER silently proceeds.
"""

from __future__ import annotations

CONVERGENCE_SAMPLE_INTERVAL_S = 15
CONVERGENCE_TIMEOUT_S = 300


def wait_for_convergence(_node: str) -> bool:  # pragma: no cover - phase 2
    raise NotImplementedError("convergence.wait_for_convergence: phase 2")
