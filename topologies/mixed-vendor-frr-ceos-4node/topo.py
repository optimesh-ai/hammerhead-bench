"""mixed-vendor-frr-ceos-4node — FRR + Arista cEOS in one snapshot.

Shape::

    r1 (FRR)  -- 10.0.12.0/30 -- r2 (cEOS)
     |                             |
     10.0.14.0/30                  10.0.23.0/30
     |                             |
    r4 (cEOS) -- 10.0.34.0/30 -- r3 (FRR)

Ring of 4 nodes, alternating FRR and Arista cEOS. OSPFv2 point-to-point
underlay (single area 0); iBGP AS 65100 full-mesh over loopbacks with
``maximum-paths 2`` so the ring geometry surfaces ECMP on every pair.

Why this matters for the bench: every other topology in this corpus is
FRR-only. This is the fixture that proves both simulators ingest an
EOS ``startup-config`` in the same snapshot alongside FRR ``frr.conf``
and still converge to the same FIB — i.e. the vendor auto-detection
pipeline works across heterogeneous subdirectories.

Benchmarks:

- Every node's FIB has 3 loopback /32s (the other three nodes), each
  with 2 next-hops by iBGP-over-OSPF ECMP (one per ring direction).
- A mixed-vendor parse failure collapses to 0 routes on one side and
  surfaces as a ``presence`` diff, not a ``next_hop_match`` diff.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.ceos import CeosAdapter
from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_ceos = CeosAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ASN = 65100
_AREA = "0.0.0.0"

# iBGP full-mesh: every node peers with every other over loopbacks.
_LOOPBACKS = {
    "r1": "10.0.0.1",
    "r2": "10.0.0.2",
    "r3": "10.0.0.3",
    "r4": "10.0.0.4",
}


def _ibgp_peers(self_name: str) -> list[dict]:
    return [
        {"asn": _ASN, "ip": ip, "description": f"ibgp to {name}"}
        for name, ip in _LOOPBACKS.items()
        if name != self_name
    ]


def _frr_node(name: str, interfaces: tuple[Interface, ...]) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "asn": _ASN,
            "loopback": _LOOPBACKS[name],
            "area": _AREA,
            "ibgp_peers": _ibgp_peers(name),
            "enabled_daemons": ["ospfd", "bgpd", "staticd"],
        },
    )


def _ceos_node(name: str, interfaces: tuple[Interface, ...]) -> Node:
    return Node(
        name=name,
        adapter=_ceos,
        interfaces=interfaces,
        params={
            "asn": _ASN,
            "loopback": _LOOPBACKS[name],
            "area": _AREA,
            "ibgp_peers": _ibgp_peers(name),
        },
    )


SPEC = TopologySpec(
    name="mixed-vendor-frr-ceos-4node",
    description="4-node FRR+cEOS ring, OSPF underlay + iBGP ECMP over loopbacks.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _frr_node(
            "r1",
            interfaces=(
                Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),
                Interface(name="eth2", ip="10.0.14.1/30", description="to r4"),
            ),
        ),
        _ceos_node(
            "r2",
            interfaces=(
                Interface(name="Ethernet1", ip="10.0.12.2/30", description="to r1"),
                Interface(name="Ethernet2", ip="10.0.23.1/30", description="to r3"),
            ),
        ),
        _frr_node(
            "r3",
            interfaces=(
                Interface(name="eth1", ip="10.0.23.2/30", description="to r2"),
                Interface(name="eth2", ip="10.0.34.2/30", description="to r4"),
            ),
        ),
        _ceos_node(
            "r4",
            interfaces=(
                Interface(name="Ethernet1", ip="10.0.14.2/30", description="to r1"),
                Interface(name="Ethernet2", ip="10.0.34.1/30", description="to r3"),
            ),
        ),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("r2", "Ethernet1")),
        Link(a=("r2", "Ethernet2"), b=("r3", "eth1")),
        Link(a=("r3", "eth2"), b=("r4", "Ethernet2")),
        Link(a=("r4", "Ethernet1"), b=("r1", "eth2")),
    ),
)
