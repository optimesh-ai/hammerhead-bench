"""spine-leaf-6node — 2 spines + 4 leaves, eBGP ECMP across the fabric.

Shape::

       S1        S2
      /  \\    /  \\    (each leaf uplinks to both spines)
    L1   L2  L3   L4

Every leaf is in its own AS; spines share AS 65000 for the fabric.

- Each leaf advertises its own loopback /32 to both spines.
- Spines redistribute across each other (acting like route reflectors
  but in eBGP-over-a-shared-AS style). To keep the model simple and
  FRR-faithful, both spines share AS 65000 and all four leaves have
  distinct ASNs (65001..65004); spine-to-spine session isn't needed
  because each leaf talks to both spines directly.
- ``maximum-paths 2`` on every leaf so the 3 remote loopbacks land
  with 2 next-hops (one per spine) and ECMP surfaces across all three
  simulators.

Benchmarks:

- Every leaf has 3 iBGP-class loopback /32s (via each spine's
  transit IP). Vendor, Batfish, Hammerhead must agree on BOTH
  next-hop entries per route.
- A missing second next-hop (ECMP collapse) surfaces as a
  ``next_hop_match = false`` row.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_SPINE_ASN = 65000


def _spine(
    name: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
    leaf_peers: list[dict],
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "role": "spine",
            "asn": _SPINE_ASN,
            "loopback": loopback,
            "leaf_peers": leaf_peers,
            "enabled_daemons": ["bgpd", "staticd"],
        },
    )


def _leaf(
    name: str,
    asn: int,
    loopback: str,
    interfaces: tuple[Interface, ...],
    spine_peers: list[dict],
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "role": "leaf",
            "asn": asn,
            "loopback": loopback,
            "spine_peers": spine_peers,
            "enabled_daemons": ["bgpd", "staticd"],
        },
    )


# Addressing plan (one /30 per leaf<->spine link):
#
# S1<->L1 10.0.11.0/30   S1<->L2 10.0.12.0/30   S1<->L3 10.0.13.0/30   S1<->L4 10.0.14.0/30
# S2<->L1 10.0.21.0/30   S2<->L2 10.0.22.0/30   S2<->L3 10.0.23.0/30   S2<->L4 10.0.24.0/30
#
# S1 on every transit: .1; leaf: .2.
# S2 on every transit: .1; leaf: .2.
SPEC = TopologySpec(
    name="spine-leaf-6node",
    description="2 spines + 4 leaves, eBGP ECMP across a classic Clos.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _spine(
            "s1",
            loopback="10.0.0.101",
            interfaces=(
                Interface(name="eth1", ip="10.0.11.1/30", description="to l1"),
                Interface(name="eth2", ip="10.0.12.1/30", description="to l2"),
                Interface(name="eth3", ip="10.0.13.1/30", description="to l3"),
                Interface(name="eth4", ip="10.0.14.1/30", description="to l4"),
            ),
            leaf_peers=[
                {"asn": 65001, "ip": "10.0.11.2"},
                {"asn": 65002, "ip": "10.0.12.2"},
                {"asn": 65003, "ip": "10.0.13.2"},
                {"asn": 65004, "ip": "10.0.14.2"},
            ],
        ),
        _spine(
            "s2",
            loopback="10.0.0.102",
            interfaces=(
                Interface(name="eth1", ip="10.0.21.1/30", description="to l1"),
                Interface(name="eth2", ip="10.0.22.1/30", description="to l2"),
                Interface(name="eth3", ip="10.0.23.1/30", description="to l3"),
                Interface(name="eth4", ip="10.0.24.1/30", description="to l4"),
            ),
            leaf_peers=[
                {"asn": 65001, "ip": "10.0.21.2"},
                {"asn": 65002, "ip": "10.0.22.2"},
                {"asn": 65003, "ip": "10.0.23.2"},
                {"asn": 65004, "ip": "10.0.24.2"},
            ],
        ),
        _leaf(
            "l1",
            asn=65001,
            loopback="10.0.0.1",
            interfaces=(
                Interface(name="eth1", ip="10.0.11.2/30", description="to s1"),
                Interface(name="eth2", ip="10.0.21.2/30", description="to s2"),
            ),
            spine_peers=[
                {"asn": _SPINE_ASN, "ip": "10.0.11.1"},
                {"asn": _SPINE_ASN, "ip": "10.0.21.1"},
            ],
        ),
        _leaf(
            "l2",
            asn=65002,
            loopback="10.0.0.2",
            interfaces=(
                Interface(name="eth1", ip="10.0.12.2/30", description="to s1"),
                Interface(name="eth2", ip="10.0.22.2/30", description="to s2"),
            ),
            spine_peers=[
                {"asn": _SPINE_ASN, "ip": "10.0.12.1"},
                {"asn": _SPINE_ASN, "ip": "10.0.22.1"},
            ],
        ),
        _leaf(
            "l3",
            asn=65003,
            loopback="10.0.0.3",
            interfaces=(
                Interface(name="eth1", ip="10.0.13.2/30", description="to s1"),
                Interface(name="eth2", ip="10.0.23.2/30", description="to s2"),
            ),
            spine_peers=[
                {"asn": _SPINE_ASN, "ip": "10.0.13.1"},
                {"asn": _SPINE_ASN, "ip": "10.0.23.1"},
            ],
        ),
        _leaf(
            "l4",
            asn=65004,
            loopback="10.0.0.4",
            interfaces=(
                Interface(name="eth1", ip="10.0.14.2/30", description="to s1"),
                Interface(name="eth2", ip="10.0.24.2/30", description="to s2"),
            ),
            spine_peers=[
                {"asn": _SPINE_ASN, "ip": "10.0.14.1"},
                {"asn": _SPINE_ASN, "ip": "10.0.24.1"},
            ],
        ),
    ),
    links=(
        Link(a=("s1", "eth1"), b=("l1", "eth1")),
        Link(a=("s1", "eth2"), b=("l2", "eth1")),
        Link(a=("s1", "eth3"), b=("l3", "eth1")),
        Link(a=("s1", "eth4"), b=("l4", "eth1")),
        Link(a=("s2", "eth1"), b=("l1", "eth2")),
        Link(a=("s2", "eth2"), b=("l2", "eth2")),
        Link(a=("s2", "eth3"), b=("l3", "eth2")),
        Link(a=("s2", "eth4"), b=("l4", "eth2")),
    ),
)
