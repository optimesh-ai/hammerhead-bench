"""Per-topology + aggregate diff metrics — Phase 4 deliverable.

Given a list of :class:`DiffRecord` rows, compute the headline numbers that
go in the benchmark report:

- ``presence_match_rate`` — fraction of (node, vrf, prefix) keys present in
  both sim and vendor.
- ``next_hop_match_rate`` — among keys present in both, fraction with set-
  equal next-hops (ECMP-insensitive).
- ``protocol_match_rate`` — ditto for protocol label.
- ``bgp_attr_match_rate`` — among BGP-protocol rows present on both sides,
  fraction with matching AS_PATH + LOCAL_PREF + MED.
- Per-protocol next-hop breakdown — one rate per protocol label so we can
  see that (e.g.) OSPF matches 98% but BGP only 91%.

The numbers are reported per-simulator (batfish, hammerhead). Each metric
answers the question "how close is SIM to VENDOR on this topology?" — we
do NOT report batfish-vs-hammerhead; that comparison is ill-defined because
both have their own bugs.

All rates are in [0.0, 1.0]. Divide-by-zero returns 1.0 (a source with no
routes present matches vendor trivially) so the aggregate average isn't
skewed by empty topologies. The rationale is recorded in the
:func:`_safe_div` docstring.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Literal

from harness.diff.engine import DiffRecord

__all__ = ["TopologyMetrics", "aggregate", "aggregate_many"]

Simulator = Literal["batfish", "hammerhead"]

# Records with this presence are the only ones used for match-rate
# denominators (both sides have the route). Derived once so additions to the
# Presence literal don't silently break the metric math.
_BOTH_SIDES_BATFISH: frozenset[str] = frozenset({"all-three", "vendor-and-batfish"})
_BOTH_SIDES_HAMMERHEAD: frozenset[str] = frozenset({"all-three", "vendor-and-hammerhead"})


@dataclass(slots=True)
class _SimRates:
    """Per-simulator rate bundle."""

    present_in_both: int = 0
    next_hop_match: int = 0
    protocol_match: int = 0
    bgp_total: int = 0
    bgp_attr_match: int = 0
    # Per-protocol next-hop correctness: protocol -> (total, matched).
    per_protocol_total: dict[str, int] = field(default_factory=dict)
    per_protocol_match: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TopologyMetrics:
    """Metrics for one topology."""

    topology: str
    total_routes_vendor: int
    total_routes_batfish: int
    total_routes_hammerhead: int
    # Per-simulator totals + match rates. Structured so reports can iterate.
    batfish_presence_match_rate: float
    batfish_next_hop_match_rate: float
    batfish_protocol_match_rate: float
    batfish_bgp_attr_match_rate: float
    batfish_per_protocol_next_hop_match_rate: dict[str, float]
    hammerhead_presence_match_rate: float
    hammerhead_next_hop_match_rate: float
    hammerhead_protocol_match_rate: float
    hammerhead_bgp_attr_match_rate: float
    hammerhead_per_protocol_next_hop_match_rate: dict[str, float]

    def as_dict(self) -> dict:
        return asdict(self)


def aggregate(topology: str, records: Iterable[DiffRecord]) -> TopologyMetrics:
    """Summarize one topology's diff records into one :class:`TopologyMetrics`."""
    records = list(records)

    vendor_total = sum(1 for r in records if r.vendor_protocol is not None)
    batfish_total = sum(1 for r in records if r.batfish_protocol is not None)
    hammerhead_total = sum(1 for r in records if r.hammerhead_protocol is not None)

    batfish = _collect(records, which="batfish")
    hammerhead = _collect(records, which="hammerhead")

    return TopologyMetrics(
        topology=topology,
        total_routes_vendor=vendor_total,
        total_routes_batfish=batfish_total,
        total_routes_hammerhead=hammerhead_total,
        batfish_presence_match_rate=_safe_div(batfish.present_in_both, vendor_total),
        batfish_next_hop_match_rate=_safe_div(batfish.next_hop_match, batfish.present_in_both),
        batfish_protocol_match_rate=_safe_div(batfish.protocol_match, batfish.present_in_both),
        batfish_bgp_attr_match_rate=_safe_div(batfish.bgp_attr_match, batfish.bgp_total),
        batfish_per_protocol_next_hop_match_rate=_per_protocol_rates(batfish),
        hammerhead_presence_match_rate=_safe_div(hammerhead.present_in_both, vendor_total),
        hammerhead_next_hop_match_rate=_safe_div(
            hammerhead.next_hop_match, hammerhead.present_in_both
        ),
        hammerhead_protocol_match_rate=_safe_div(
            hammerhead.protocol_match, hammerhead.present_in_both
        ),
        hammerhead_bgp_attr_match_rate=_safe_div(hammerhead.bgp_attr_match, hammerhead.bgp_total),
        hammerhead_per_protocol_next_hop_match_rate=_per_protocol_rates(hammerhead),
    )


def aggregate_many(per_topology: Iterable[TopologyMetrics]) -> dict:
    """Simple mean across topologies for the headline numbers.

    Returned dict is flat and JSON-serializable so reports can render it with
    zero string munging.
    """
    metrics = list(per_topology)
    n = len(metrics)
    if n == 0:
        return {
            "topology_count": 0,
            "batfish_next_hop_match_rate_mean": 1.0,
            "hammerhead_next_hop_match_rate_mean": 1.0,
        }
    return {
        "topology_count": n,
        "batfish_presence_match_rate_mean": _mean(m.batfish_presence_match_rate for m in metrics),
        "batfish_next_hop_match_rate_mean": _mean(m.batfish_next_hop_match_rate for m in metrics),
        "batfish_protocol_match_rate_mean": _mean(m.batfish_protocol_match_rate for m in metrics),
        "batfish_bgp_attr_match_rate_mean": _mean(m.batfish_bgp_attr_match_rate for m in metrics),
        "hammerhead_presence_match_rate_mean": _mean(
            m.hammerhead_presence_match_rate for m in metrics
        ),
        "hammerhead_next_hop_match_rate_mean": _mean(
            m.hammerhead_next_hop_match_rate for m in metrics
        ),
        "hammerhead_protocol_match_rate_mean": _mean(
            m.hammerhead_protocol_match_rate for m in metrics
        ),
        "hammerhead_bgp_attr_match_rate_mean": _mean(
            m.hammerhead_bgp_attr_match_rate for m in metrics
        ),
    }


# ---- helpers -------------------------------------------------------------


def _collect(records: Iterable[DiffRecord], *, which: Simulator) -> _SimRates:
    """Walk records once, counting the bits needed for one simulator's rates."""
    rates = _SimRates()
    both_sides = _BOTH_SIDES_BATFISH if which == "batfish" else _BOTH_SIDES_HAMMERHEAD
    nh_attr = "batfish_next_hop_match" if which == "batfish" else "hammerhead_next_hop_match"
    proto_attr = "batfish_protocol_match" if which == "batfish" else "hammerhead_protocol_match"
    bgp_attr = "batfish_bgp_attrs_match" if which == "batfish" else "hammerhead_bgp_attrs_match"
    sim_proto_attr = "batfish_protocol" if which == "batfish" else "hammerhead_protocol"

    per_total: Counter[str] = Counter()
    per_match: Counter[str] = Counter()

    for r in records:
        if r.presence not in both_sides:
            continue
        rates.present_in_both += 1
        if getattr(r, nh_attr) is True:
            rates.next_hop_match += 1
        if getattr(r, proto_attr) is True:
            rates.protocol_match += 1
        # BGP attrs only count when BOTH sides are BGP. bgp_attrs_match
        # collapses that to None otherwise.
        battr = getattr(r, bgp_attr)
        if battr is not None:
            rates.bgp_total += 1
            if battr is True:
                rates.bgp_attr_match += 1
        # Per-protocol next-hop: key off the VENDOR protocol so per-protocol
        # denominators are vendor-authoritative.
        proto = r.vendor_protocol
        if proto is None:
            # Vendor-missing keys can't be on both sides; presence filter
            # above guaranteed vendor is present, so this is defensive.
            proto = getattr(r, sim_proto_attr) or "unknown"
        per_total[proto] += 1
        if getattr(r, nh_attr) is True:
            per_match[proto] += 1

    rates.per_protocol_total = dict(per_total)
    rates.per_protocol_match = dict(per_match)
    return rates


def _per_protocol_rates(r: _SimRates) -> dict[str, float]:
    return {
        proto: _safe_div(r.per_protocol_match.get(proto, 0), total)
        for proto, total in sorted(r.per_protocol_total.items())
    }


def _safe_div(num: int, denom: int) -> float:
    """Return ``num/denom`` as a float in [0.0, 1.0], or 1.0 if denom is zero.

    Rationale for the 1.0 fallback: an empty set trivially matches itself.
    Reports use a separate ``total_routes_*`` field to surface empty
    topologies — a 1.0 rate next to a zero total is self-describing.
    """
    if denom == 0:
        return 1.0
    return num / denom


def _mean(values: Iterable[float]) -> float:
    xs = list(values)
    return sum(xs) / len(xs) if xs else 1.0
