"""hub-spoke-wan-51node — 1 hub + 50 branches, eBGP WAN star.

Shape::

                  ┌─ b02 (AS 65002)
                  ├─ b03 (AS 65003)
    hub (AS 65000)├─ ...
                  ├─ b50 (AS 65050)
                  └─ b51 (AS 65051)

Every branch peers eBGP to the hub on its own /30 transit link; hub
advertises its loopback /32 plus a default route downstream, branches
advertise their site /24. No transit between branches; traffic
between sites always goes through the hub (classic WAN star).

Why this matters for the bench: every other corpus topology is a Clos
/ mesh / small chain. Enterprise WANs are stars. This is the fixture
that proves the benchmark isn't implicitly specialised to DC-style
symmetric fabrics — Hammerhead and Batfish must both scale to
N-peer single-node BGP session counts (51 × eBGP sessions on the hub
alone).

Benchmarks:

- Hub FIB carries N-1 branch /24 prefixes (one per non-self branch),
  next-hop via the matching /30 transit. No BGP attribute diff.
- Every branch carries the hub /32 + N-1 peer /24s (re-advertised by
  the hub), via the hub transit IP.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_HUB_ASN = 65000
_NUM_BRANCHES = 50

_DAEMONS = ["bgpd", "staticd"]


def _build_spec() -> TopologySpec:
    hub_ifaces: list[Interface] = []
    hub_peers: list[dict] = []
    branches: list[Node] = []
    links: list[Link] = []

    for i in range(1, _NUM_BRANCHES + 1):
        # /30 transit per branch: 10.10.<i>.0/30, hub=.1, branch=.2
        # Branch site prefix: 10.<20 + i>.0.0/24 (keeps it disjoint from
        # transit space at 10.10.*).
        hub_ip = f"10.10.{i}.1/30"
        br_ip = f"10.10.{i}.2/30"
        br_loopback = f"10.20.{i}.1"
        br_site_prefix = f"10.{20 + i}.0.0/24"

        hub_iface = Interface(
            name=f"eth{i}",
            ip=hub_ip,
            description=f"to b{i:02d}",
        )
        hub_ifaces.append(hub_iface)
        hub_peers.append(
            {
                "asn": _HUB_ASN + i,
                "ip": br_ip.split("/")[0],
                "description": f"ebgp to b{i:02d}",
            }
        )

        br_iface = Interface(name="eth1", ip=br_ip, description="to hub")
        branches.append(
            Node(
                name=f"b{i:02d}",
                adapter=_frr,
                interfaces=(br_iface,),
                params={
                    "role": "branch",
                    "asn": _HUB_ASN + i,
                    "loopback": br_loopback,
                    "site_prefix": br_site_prefix,
                    "hub_asn": _HUB_ASN,
                    "hub_ip": hub_ip.split("/")[0],
                    "enabled_daemons": _DAEMONS,
                },
            )
        )
        links.append(Link(a=("hub", f"eth{i}"), b=(f"b{i:02d}", "eth1")))

    hub = Node(
        name="hub",
        adapter=_frr,
        interfaces=tuple(hub_ifaces),
        params={
            "role": "hub",
            "asn": _HUB_ASN,
            "loopback": "10.0.0.1",
            "default_advertisement": "0.0.0.0/0",
            "hub_peers": hub_peers,
            "enabled_daemons": _DAEMONS,
        },
    )

    return TopologySpec(
        name="hub-spoke-wan-51node",
        description=f"1 hub + {_NUM_BRANCHES} branches, eBGP over /30 transits.",
        template_dir=_TEMPLATE_DIR,
        nodes=(hub, *branches),
        links=tuple(links),
    )


SPEC = _build_spec()
