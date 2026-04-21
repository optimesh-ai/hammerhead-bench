"""route-reflector-6node — 2 RRs + 4 clients, single AS, loopback-sourced iBGP.

Shape (logical, iBGP sessions; physical is a shared L2 bridge)::

    C1 ---\\                  /--- C3
            RR1 --- RR2
    C2 ---/                  \\--- C4

Physical: all six routers share one 10.0.99.0/24 broadcast segment via a
clab ``bridge`` node (``hub``). Sidesteps needing a full L2 fabric — every
router can reach every other router's loopback via static route +
connected-route recursion over the shared segment, which is enough for
loopback-sourced iBGP to come up.

RR topology:

- RR1 has cluster-id ``1.1.1.1`` and reflects between C1/C2/C3/C4 and RR2.
- RR2 has cluster-id ``2.2.2.2`` and reflects between C1/C2/C3/C4 and RR1.
- Each client peers only with both RRs (not with each other).

Each client advertises its own loopback /32. A well-behaved RR setup
means every client sees exactly 3 remote loopbacks (the other three).

Benchmarks:

- Each client FIB carries 3 iBGP-learned /32 prefixes; CLUSTER_LIST has
  the originating RR's cluster-id exactly once; ORIGINATOR_ID matches
  the originating client's router-id.
- No loop: a client never receives its own prefix reflected back.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.bridge import BridgeAdapter
from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_bridge = BridgeAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ASN = 65100
_SEGMENT_CIDR = "10.0.99.0/24"

_CLIENT_DAEMONS = ["bgpd", "staticd"]
_RR_DAEMONS = ["bgpd", "staticd"]

# (loopback, segment_ip) for every router on the shared fabric.
_FABRIC = {
    "c1": ("10.0.0.11", "10.0.99.11"),
    "c2": ("10.0.0.12", "10.0.99.12"),
    "c3": ("10.0.0.13", "10.0.99.13"),
    "c4": ("10.0.0.14", "10.0.99.14"),
    "rr1": ("10.0.0.101", "10.0.99.101"),
    "rr2": ("10.0.0.102", "10.0.99.102"),
}


def _peer_routes(self_name: str) -> list[dict]:
    """Return [{'loopback': ..., 'segment_ip': ...}, ...] for every other router."""
    return [
        {"loopback": lo, "segment_ip": seg}
        for name, (lo, seg) in _FABRIC.items()
        if name != self_name
    ]


def _client(name: str, ip: str, loopback: str, rr_peers: list[str]) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=(Interface(name="eth1", ip=f"{ip}/24", description="shared segment"),),
        params={
            "role": "client",
            "asn": _ASN,
            "loopback": loopback,
            "segment_ip": ip,
            "segment_cidr": _SEGMENT_CIDR,
            "rr_peers": rr_peers,
            "peer_routes": _peer_routes(name),
            "enabled_daemons": _CLIENT_DAEMONS,
        },
    )


def _rr(
    name: str,
    ip: str,
    loopback: str,
    cluster_id: str,
    client_peers: list[str],
    rr_peer: str,
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=(Interface(name="eth1", ip=f"{ip}/24", description="shared segment"),),
        params={
            "role": "rr",
            "asn": _ASN,
            "loopback": loopback,
            "segment_ip": ip,
            "segment_cidr": _SEGMENT_CIDR,
            "cluster_id": cluster_id,
            "client_peers": client_peers,
            "rr_peer": rr_peer,
            "peer_routes": _peer_routes(name),
            "enabled_daemons": _RR_DAEMONS,
        },
    )


SPEC = TopologySpec(
    name="route-reflector-6node",
    description="iBGP RR cluster: 2 RRs + 4 clients, loopback-sourced.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _client("c1", "10.0.99.11", "10.0.0.11", rr_peers=["10.0.0.101", "10.0.0.102"]),
        _client("c2", "10.0.99.12", "10.0.0.12", rr_peers=["10.0.0.101", "10.0.0.102"]),
        _client("c3", "10.0.99.13", "10.0.0.13", rr_peers=["10.0.0.101", "10.0.0.102"]),
        _client("c4", "10.0.99.14", "10.0.0.14", rr_peers=["10.0.0.101", "10.0.0.102"]),
        _rr(
            "rr1",
            "10.0.99.101",
            "10.0.0.101",
            cluster_id="1.1.1.1",
            client_peers=["10.0.0.11", "10.0.0.12", "10.0.0.13", "10.0.0.14"],
            rr_peer="10.0.0.102",
        ),
        _rr(
            "rr2",
            "10.0.99.102",
            "10.0.0.102",
            cluster_id="2.2.2.2",
            client_peers=["10.0.0.11", "10.0.0.12", "10.0.0.13", "10.0.0.14"],
            rr_peer="10.0.0.101",
        ),
        Node(name="hub", adapter=_bridge, interfaces=(), params={}),
    ),
    links=(
        Link(a=("c1", "eth1"), b=("hub", "eth1")),
        Link(a=("c2", "eth1"), b=("hub", "eth2")),
        Link(a=("c3", "eth1"), b=("hub", "eth3")),
        Link(a=("c4", "eth1"), b=("hub", "eth4")),
        Link(a=("rr1", "eth1"), b=("hub", "eth5")),
        Link(a=("rr2", "eth1"), b=("hub", "eth6")),
    ),
)
