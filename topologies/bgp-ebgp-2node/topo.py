"""bgp-ebgp-2node — two routers in different ASes, eBGP over a directly connected link.

Shape::

    r1 (AS 65001, lo 10.0.0.1/32) -- eth1 10.0.12.0/30 eth1 -- r2 (AS 65002, lo 10.0.0.2/32)

No loopback peering, no multihop, no inbound or outbound policy. Each side
advertises its own loopback via ``network <lo>/32``. The BGP session runs
directly between the transit /30 IPs so the session is up without an IGP.

This topology is a strict RFC 4271 §5.1.1 tripwire: LOCAL_PREF must NOT
cross an eBGP boundary. Any implementation that leaks LOCAL_PREF onto the
wire surfaces here as a ``bgp_attrs_match = false`` row in the diff.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _node(name: str, asn: int, loopback: str, ip: str, peer_asn: int, peer_ip: str) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=(Interface(name="eth1", ip=f"{ip}/30", description="ebgp transit"),),
        params={
            "asn": asn,
            "loopback": loopback,
            "peer_asn": peer_asn,
            "peer_ip": peer_ip,
            "enabled_daemons": ["bgpd", "staticd"],
        },
    )


SPEC = TopologySpec(
    name="bgp-ebgp-2node",
    description="Two-router eBGP over a /30 transit, each side in a distinct AS.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _node(
            "r1",
            asn=65001,
            loopback="10.0.0.1",
            ip="10.0.12.1",
            peer_asn=65002,
            peer_ip="10.0.12.2",
        ),
        _node(
            "r2",
            asn=65002,
            loopback="10.0.0.2",
            ip="10.0.12.2",
            peer_asn=65001,
            peer_ip="10.0.12.1",
        ),
    ),
    links=(Link(a=("r1", "eth1"), b=("r2", "eth1")),),
)
