"""Pure transform: Hammerhead CLI JSON -> canonical ``NodeFib``.

Kept out of ``hammerhead.py`` so the transform has its own test surface
(``tests/test_hammerhead_transform.py``). When Hammerhead's output schema
evolves, this is the single place that needs to change.

Shape contract (subset of ``hammerhead rib --device <X> --format json``):

.. code-block:: json

    {
      "hostname": "r1",
      "entries": [
        {
          "prefix": "10.0.0.0/24",
          "protocol": "B",
          "admin_distance": 200,
          "metric": 0,
          "next_hop_interface": "eth0",
          "next_hop_ip": "10.0.12.2",
          "tag": 0,
          "bgp": {"as_path": [65001, 65002], "local_preference": 100,
                  "med": 0, "origin": "igp", "communities": ["65001:1"],
                  "weight": 0},
          "ospf": null
        }
      ]
    }

Protocol codes follow Hammerhead's `output.rs::protocol_code`:
- ``C`` → connected
- ``S`` → static
- ``B`` → bgp (BgpExternal + BgpInternal both collapse to B)
- ``O`` / ``O IA`` / ``O E1`` / ``O E2`` → ospf
- ``i L1`` / ``i L2`` → isis
- ``R`` / ``R6`` → rip

Unknown codes raise ``ValueError`` so schema drift in the Rust side is
loud — silently dropping routes is a correctness bug we MUST see.
"""

from __future__ import annotations

from typing import Any

from harness.extract.fib import (
    NextHop,
    NodeFib,
    Route,
    canonicalize_vrf,
)
from harness.extract.fib import (
    Protocol as _FibProtocol,
)

__all__ = ["transform_rib_view"]


# Hammerhead → canonical FIB protocol map. Keep explicit so an added
# protocol code in the Rust side raises here instead of being silently
# dropped.
_HAMMERHEAD_PROTOCOL_MAP: dict[str, _FibProtocol] = {
    "C": "connected",
    "S": "static",
    "B": "bgp",
    "O": "ospf",
    "O IA": "ospf",
    "O E1": "ospf",
    "O E2": "ospf",
    "i L1": "isis",
    "i L2": "isis",
    "R": "rip",
    # "L" collides with Hammerhead's LDP label, which we don't benchmark;
    # drop it here rather than mapping to anything.
}
# Codes we choose to drop silently because they aren't in the benchmark
# scope. Anything not in MAP and not in SKIP raises.
_HAMMERHEAD_PROTOCOL_SKIP: frozenset[str] = frozenset(
    {
        "L",          # LDP — not benchmarked
        "T",          # RSVP-TE — not benchmarked
        "SR",         # SR — not benchmarked
        "SR-TE",      # SR-TE — not benchmarked
        "SR6",        # SRv6 — not benchmarked
        "R6",         # RIPng — not benchmarked in v1
        "D",          # EIGRP — not benchmarked in v1
        "D EX",       # EIGRP external — not benchmarked in v1
        "D6",         # EIGRPv6 — not benchmarked in v1
        "D6 EX",      # EIGRPv6 external — not benchmarked in v1
        "Bd",         # Bidir-PIM — not benchmarked
        "M",          # MSDP — not benchmarked
    }
)


def transform_rib_view(
    view: dict[str, Any],
    *,
    vrf: str = "default",
) -> NodeFib:
    """Convert one device's ``hammerhead rib --format json`` output to a NodeFib.

    ``vrf`` is a parameter because the Rust rib command flattens all VRFs
    into one entries list. For v1 benchmark topologies we run everything
    in the default VRF; when we add VRF-aware topologies the caller will
    pass the VRF explicitly (by re-running rib per VRF, or by teaching
    the Rust side to emit a VRF field).

    Raises ``ValueError`` for unknown protocol codes so schema drift
    surfaces loudly.
    """
    hostname = str(view.get("hostname") or "").strip()
    if not hostname:
        raise ValueError("rib view missing 'hostname'")
    raw_entries = view.get("entries")
    if not isinstance(raw_entries, list):
        return NodeFib(
            node=hostname,
            vrf=canonicalize_vrf(vrf),
            source="hammerhead",
            routes=[],
        )

    routes: list[Route] = []
    for e in raw_entries:
        if not isinstance(e, dict):
            continue
        parsed = _parse_entry(e)
        if parsed is not None:
            routes.append(parsed)
    return NodeFib(
        node=hostname,
        vrf=canonicalize_vrf(vrf),
        source="hammerhead",
        routes=routes,
    )


def _parse_entry(entry: dict[str, Any]) -> Route | None:
    prefix = str(entry.get("prefix") or "").strip()
    if not prefix:
        return None

    raw_proto = str(entry.get("protocol") or "").strip()
    protocol = _map_protocol(raw_proto)
    if protocol is None:
        return None

    next_hops = _next_hops(entry)

    bgp = entry.get("bgp") if isinstance(entry.get("bgp"), dict) else None
    # `X or Y` falls through on 0; BGP MED / LOCAL_PREF can legitimately
    # be 0, so pick the first *present* key.
    as_path = _as_int_list(bgp.get("as_path")) if bgp else None
    local_pref = _as_int(bgp.get("local_preference")) if bgp else None
    med = _as_int(bgp.get("med")) if bgp else None
    communities = _communities(bgp) if bgp else None

    return Route(
        prefix=prefix,
        protocol=protocol,
        next_hops=next_hops,
        admin_distance=_as_int(entry.get("admin_distance")),
        metric=_as_int(entry.get("metric")),
        as_path=as_path,
        local_pref=local_pref,
        med=med,
        communities=communities,
    )


def _map_protocol(raw: str) -> _FibProtocol | None:
    """Return the canonical protocol, or None if the code is explicitly
    skipped. Raises ``ValueError`` on an unknown code."""
    if raw in _HAMMERHEAD_PROTOCOL_SKIP:
        return None
    mapped = _HAMMERHEAD_PROTOCOL_MAP.get(raw)
    if mapped is None:
        raise ValueError(
            f"unknown Hammerhead protocol code {raw!r}; "
            "update _HAMMERHEAD_PROTOCOL_MAP or _HAMMERHEAD_PROTOCOL_SKIP"
        )
    return mapped


def _next_hops(entry: dict[str, Any]) -> list[NextHop]:
    """Extract next-hops from a single rib entry.

    Hammerhead's rib command emits one (next_hop_interface, next_hop_ip)
    pair per entry (no ECMP array at this layer — ECMP routes show up as
    multiple entries with the same prefix + different next-hops). Both
    fields may be None (for discard / connected routes).
    """
    ip = entry.get("next_hop_ip")
    iface = entry.get("next_hop_interface")
    # Normalize "0.0.0.0" discard marker to None.
    if isinstance(ip, str) and ip.strip() == "0.0.0.0":
        ip = None
    if ip is None and iface is None:
        return []
    return [NextHop(ip=ip if ip else None, interface=iface if iface else None)]


def _as_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _as_int_list(val: Any) -> list[int] | None:
    if val is None:
        return None
    if isinstance(val, list):
        out: list[int] = []
        for x in val:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(val, str):
        out2: list[int] = []
        for tok in val.strip().split():
            try:
                out2.append(int(tok))
            except ValueError:
                continue
        return out2
    return None


def _communities(bgp: dict[str, Any]) -> list[str] | None:
    """Merge standard + extended communities into one string list.

    The Rust side already renders each community as a display string
    (``65001:1``, ``no-export``, ``rt 65000:1``) so we just concatenate.
    Ordering: standard first, then extended, preserving Rust's emission
    order so round-trip comparisons against Batfish output stay stable.
    """
    out: list[str] = []
    for k in ("communities", "ext_communities"):
        vs = bgp.get(k)
        if isinstance(vs, list):
            for v in vs:
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
    return out or None
