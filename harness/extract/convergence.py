"""Convergence detection shared constants.

The actual poll loop lives in each vendor adapter because the poll mechanism
is vendor-specific (vtysh for FRR, eAPI for cEOS, NETCONF for Junos). These
constants codify the spec across all vendors so a future adapter can't pick
a different cadence by accident.

Contract (spec):

- A node is converged when ALL configured BGP sessions are Established AND
  the total route count is identical across two consecutive 15 s samples.
- Hard cap: 5 min per node. Failure aborts the topology loudly; NEVER silently
  proceeds.
"""

from __future__ import annotations

CONVERGENCE_SAMPLE_INTERVAL_S = 15
CONVERGENCE_TIMEOUT_S = 300
