"""multi-as-edge-5node — classic ISP edge: 1 local + 2 upstreams + 1 peer + 1 customer.

Shape::

              upstream_a (AS 100)     upstream_b (AS 200)
                     \\                   /
                      \\                 /
                       local (AS 65000)
                      /                 \\
                     /                   \\
              peer (AS 300)         customer (AS 65001)

Five eBGP sessions on ``local``, each on its own /30 transit. Each remote AS
originates its own /24 so the local router must apply vendor-correct policy
to pick the winning path and the correct set of prefixes leaks to each
neighbor class.

Policy on ``local``:

- Inbound LOCAL_PREF: customer=200, peer=150, upstream=100 (default).
  Per RFC 4271 §5.1.1, local-preference is the first real tie-break in
  the BGP decision process after weight — making customer the
  most-preferred exit.
- Outbound advertise set:
  - To customers: everything (full table analog).
  - To peers: own + customer routes only (no transit — "cold potato"
    to prevent settlement-free peers from using us as transit).
  - To upstreams: own + customer routes only (same reason).

Benchmarks:

- ``local`` FIB carries 5 prefixes: own /24, upstream_a /24, upstream_b /24,
  peer /24, customer /24. Upstream prefixes next-hop the upstream transit
  IP (LP=100); peer /24 next-hops the peer transit IP (LP=150); customer
  /24 wins LP=200.
- ``peer`` sees only {local, customer} /24s (upstream /24s must NOT
  leak — this is the headline correctness property of peering policy).
- ``upstream_a`` and ``upstream_b`` each see {local, customer} /24s.
- ``customer`` sees all five prefixes (its own + everything local knows).

Why this matters for the bench: every real internet-edge router enforces
this exact policy matrix. Getting it wrong costs money (paying transit
for peer traffic) or starts BGP incidents (leaking transit to peers,
pulling others' traffic). Hammerhead and Batfish must both compute the
same four FIBs from the same set of route-maps.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_LOCAL_ASN = 65000
_UPA_ASN = 100
_UPB_ASN = 200
_PEER_ASN = 300
_CUST_ASN = 65001

_DAEMONS = ["bgpd", "staticd"]


def _neighbor(
    asn: int, transit_ip: str, description: str, peer_class: str
) -> dict:
    return {
        "asn": asn,
        "ip": transit_ip,
        "description": description,
        "peer_class": peer_class,
    }


SPEC = TopologySpec(
    name="multi-as-edge-5node",
    description="Internet edge: local ISP + 2 upstreams + 1 peer + 1 customer, classic policy matrix.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        Node(
            name="local",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.10.1/30", description="to upstream_a"),
                Interface(name="eth2", ip="10.0.20.1/30", description="to upstream_b"),
                Interface(name="eth3", ip="10.0.30.1/30", description="to peer"),
                Interface(name="eth4", ip="10.0.40.1/30", description="to customer"),
            ),
            params={
                "role": "edge",
                "asn": _LOCAL_ASN,
                "loopback": "10.0.0.1",
                "originate_prefix": "203.0.113.0/24",
                "neighbors": [
                    _neighbor(_UPA_ASN, "10.0.10.2", "ebgp to upstream_a", "upstream"),
                    _neighbor(_UPB_ASN, "10.0.20.2", "ebgp to upstream_b", "upstream"),
                    _neighbor(_PEER_ASN, "10.0.30.2", "ebgp to peer", "peer"),
                    _neighbor(_CUST_ASN, "10.0.40.2", "ebgp to customer", "customer"),
                ],
                "enabled_daemons": _DAEMONS,
            },
        ),
        Node(
            name="upstream_a",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.10.2/30", description="to local"),
            ),
            params={
                "role": "simple",
                "asn": _UPA_ASN,
                "loopback": "10.1.0.1",
                "originate_prefix": "198.51.100.0/24",
                "peer_ip": "10.0.10.1",
                "peer_asn": _LOCAL_ASN,
                "peer_description": "ebgp to local",
                "enabled_daemons": _DAEMONS,
            },
        ),
        Node(
            name="upstream_b",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.20.2/30", description="to local"),
            ),
            params={
                "role": "simple",
                "asn": _UPB_ASN,
                "loopback": "10.2.0.1",
                "originate_prefix": "192.0.2.0/24",
                "peer_ip": "10.0.20.1",
                "peer_asn": _LOCAL_ASN,
                "peer_description": "ebgp to local",
                "enabled_daemons": _DAEMONS,
            },
        ),
        Node(
            name="peer",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.30.2/30", description="to local"),
            ),
            params={
                "role": "simple",
                "asn": _PEER_ASN,
                "loopback": "10.3.0.1",
                "originate_prefix": "198.18.0.0/24",
                "peer_ip": "10.0.30.1",
                "peer_asn": _LOCAL_ASN,
                "peer_description": "ebgp to local",
                "enabled_daemons": _DAEMONS,
            },
        ),
        Node(
            name="customer",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.40.2/30", description="to local"),
            ),
            params={
                "role": "simple",
                "asn": _CUST_ASN,
                "loopback": "10.4.0.1",
                "originate_prefix": "203.0.114.0/24",
                "peer_ip": "10.0.40.1",
                "peer_asn": _LOCAL_ASN,
                "peer_description": "ebgp to local",
                "enabled_daemons": _DAEMONS,
            },
        ),
    ),
    links=(
        Link(a=("local", "eth1"), b=("upstream_a", "eth1")),
        Link(a=("local", "eth2"), b=("upstream_b", "eth1")),
        Link(a=("local", "eth3"), b=("peer", "eth1")),
        Link(a=("local", "eth4"), b=("customer", "eth1")),
    ),
)
