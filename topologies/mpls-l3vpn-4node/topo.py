"""mpls-l3vpn-4node — OSPF + LDP underlay, MP-BGP VPNv4 overlay, one RED VRF.

Shape::

    PE1 -- P1 -- P2 -- PE2
    (one VRF "RED" with RD/RT 100:1 on each PE)

Underlay:

- OSPFv2 area 0 on every /30 transit link + every /32 loopback.
- LDP on every interface (we set ``mpls ldp`` globally + ``mpls ldp-sync``
  on interfaces) — but FRR's LDP support is variable; for the benchmark
  we only care about the BGP-VPN *control plane* reaching the remote PE
  and the vendor/Hammerhead/Batfish FIBs showing the same VPN prefix.
- P routers do NOT run BGP — classic "BGP-free core".

Overlay:

- PE1 and PE2 run ``address-family ipv4 vpn`` (VPNv4) over a loopback-
  sourced iBGP session, AS 65100.
- Each PE has one CE-facing interface configured in VRF RED with a /32
  site prefix advertised into VRF RED.

Benchmarks:

- PE1's VRF RED FIB carries PE2's site prefix (200.0.2.0/24), next-hop
  via P1 recursively resolved through the IGP.
- PE1's *global* RIB does NOT carry 200.0.2.0/24.
- Vendor + Hammerhead + Batfish all agree on the RD-qualified prefix.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ASN = 65100
_RD = "100:1"
_RT = "100:1"

_PE_DAEMONS = ["ospfd", "ldpd", "bgpd", "staticd"]
_P_DAEMONS = ["ospfd", "ldpd", "staticd"]


def _pe(
    name: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
    site_prefix: str,
    peer_loopback: str,
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "role": "pe",
            "asn": _ASN,
            "loopback": loopback,
            "site_prefix": site_prefix,
            "peer_loopback": peer_loopback,
            "rd": _RD,
            "rt": _RT,
            "enabled_daemons": _PE_DAEMONS,
        },
    )


def _p(name: str, loopback: str, interfaces: tuple[Interface, ...]) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "role": "p",
            "loopback": loopback,
            "enabled_daemons": _P_DAEMONS,
        },
    )


SPEC = TopologySpec(
    name="mpls-l3vpn-4node",
    description="PE1 - P1 - P2 - PE2 with one RED VRF, OSPF+LDP underlay, MP-BGP overlay.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _pe(
            "pe1",
            loopback="10.0.0.1",
            interfaces=(
                # eth1 is the P-facing transit link (MPLS-enabled, OSPF+LDP).
                Interface(name="eth1", ip="10.0.12.1/30", description="to p1"),
                # eth2 is the CE-facing site interface in VRF RED.
                Interface(name="eth2", ip="192.0.2.1/30", description="to ce1 (VRF RED)"),
            ),
            site_prefix="192.0.2.0/30",
            peer_loopback="10.0.0.4",
        ),
        _p(
            "p1",
            loopback="10.0.0.2",
            interfaces=(
                Interface(name="eth1", ip="10.0.12.2/30", description="to pe1"),
                Interface(name="eth2", ip="10.0.23.1/30", description="to p2"),
            ),
        ),
        _p(
            "p2",
            loopback="10.0.0.3",
            interfaces=(
                Interface(name="eth1", ip="10.0.23.2/30", description="to p1"),
                Interface(name="eth2", ip="10.0.34.1/30", description="to pe2"),
            ),
        ),
        _pe(
            "pe2",
            loopback="10.0.0.4",
            interfaces=(
                Interface(name="eth1", ip="10.0.34.2/30", description="to p2"),
                Interface(name="eth2", ip="200.0.2.1/30", description="to ce2 (VRF RED)"),
            ),
            site_prefix="200.0.2.0/30",
            peer_loopback="10.0.0.1",
        ),
    ),
    links=(
        Link(a=("pe1", "eth1"), b=("p1", "eth1")),
        Link(a=("p1", "eth2"), b=("p2", "eth1")),
        Link(a=("p2", "eth2"), b=("pe2", "eth1")),
    ),
)
