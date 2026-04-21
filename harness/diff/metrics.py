"""Per-topology + aggregate metrics — Phase 4 deliverable.

Metrics reported:
- next_hop_match_rate (batfish-vs-vendor, hammerhead-vs-vendor)
- presence_match_rate
- bgp_attr_match_rate
- per-protocol breakdown of next_hop_match_rate
- parse_coverage: lines_in_config / lines_parsed (per tool)
"""

from __future__ import annotations


def aggregate(*_args, **_kwargs) -> dict:  # pragma: no cover - phase 4
    raise NotImplementedError("diff.metrics.aggregate: phase 4")
