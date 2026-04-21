"""route-map-pathological — iBGP triangle, route-maps rewrite LOCAL_PREF + community.

Shape::

    r1 (AS 65100, lo 10.0.0.1)
         |           |
     (iBGP)       (iBGP)
         |           |
    r2 (AS 65100) -- iBGP -- r3 (AS 65100)

All three in one AS, full iBGP mesh over loopbacks. R1 originates
``203.0.113.0/24``. R2 applies inbound ``rm-TAG200`` on sessions from r1
which stamps community ``65000:100`` and sets LOCAL_PREF = 200. R3 applies
inbound ``rm-TAG150`` on sessions from r1 which stamps community ``65000:200``
and sets LOCAL_PREF = 150.

Over the iBGP mesh, r2's advertisement to r3 still carries LP=200 (iBGP
carries LP) and r3's to r2 carries LP=150. Best path at r2: its own
r1-direct with LP=200 wins. Best path at r3: r2-reflected with LP=200
wins over its direct-from-r1 LP=150.

Benchmarks:

- At r2, the best path to 203.0.113.0/24 has LP=200 and community 65000:100.
- At r3, the best path to 203.0.113.0/24 has LP=200 (via r2) and carries
  both communities (65000:100 set by r2, 65000:200 set by r3) — OR just
  65000:100 if the r2->r3 advertisement strips the r3-side community
  because the path came via r2 first. Either interpretation is legal —
  the diff asserts Hammerhead matches FRR exactly.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ASN = 65100

# r1-r2 over 10.0.12.0/30, r1-r3 over 10.0.13.0/30, r2-r3 over 10.0.23.0/30.
# Each router has direct static routes to the other two loopbacks so iBGP
# comes up without an IGP (identical pattern to bgp-ibgp-2node).
SPEC = TopologySpec(
    name="route-map-pathological",
    description="iBGP triangle with LOCAL_PREF + community route-maps on r2 and r3.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        Node(
            name="r1",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),
                Interface(name="eth2", ip="10.0.13.1/30", description="to r3"),
            ),
            params={
                "role": "origin",
                "asn": _ASN,
                "loopback": "10.0.0.1",
                "peers": [
                    {"loopback": "10.0.0.2", "transit_ip": "10.0.12.2"},
                    {"loopback": "10.0.0.3", "transit_ip": "10.0.13.2"},
                ],
                "originate_prefix": "203.0.113.0/24",
                "route_map": None,
                "route_map_set_lp": None,
                "route_map_set_community": None,
                "enabled_daemons": ["bgpd", "staticd"],
            },
        ),
        Node(
            name="r2",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.12.2/30", description="to r1"),
                Interface(name="eth2", ip="10.0.23.1/30", description="to r3"),
            ),
            params={
                "role": "policy",
                "asn": _ASN,
                "loopback": "10.0.0.2",
                "peers": [
                    {"loopback": "10.0.0.1", "transit_ip": "10.0.12.1"},
                    {"loopback": "10.0.0.3", "transit_ip": "10.0.23.2"},
                ],
                "policy_peer": "10.0.0.1",
                "route_map": "rm-TAG200",
                "route_map_set_lp": 200,
                "route_map_set_community": "65000:100",
                "originate_prefix": None,
                "enabled_daemons": ["bgpd", "staticd"],
            },
        ),
        Node(
            name="r3",
            adapter=_frr,
            interfaces=(
                Interface(name="eth1", ip="10.0.13.2/30", description="to r1"),
                Interface(name="eth2", ip="10.0.23.2/30", description="to r2"),
            ),
            params={
                "role": "policy",
                "asn": _ASN,
                "loopback": "10.0.0.3",
                "peers": [
                    {"loopback": "10.0.0.1", "transit_ip": "10.0.13.1"},
                    {"loopback": "10.0.0.2", "transit_ip": "10.0.23.1"},
                ],
                "policy_peer": "10.0.0.1",
                "route_map": "rm-TAG150",
                "route_map_set_lp": 150,
                "route_map_set_community": "65000:200",
                "originate_prefix": None,
                "enabled_daemons": ["bgpd", "staticd"],
            },
        ),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("r2", "eth1")),
        Link(a=("r1", "eth2"), b=("r3", "eth1")),
        Link(a=("r2", "eth2"), b=("r3", "eth2")),
    ),
)
