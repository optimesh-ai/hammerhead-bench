"""bgp-ibgp-2node — two-router iBGP, loopback-sourced session over a transit /30.

Shape:

    r1 (lo 10.0.0.1/32) ---- eth1 -- 10.0.12.0/30 -- eth1 ---- r2 (lo 10.0.0.2/32)
    AS 65100                                                   AS 65100

Each router:
- Configures eth1 with its /30 transit IP and lo with its /32 loopback.
- Adds a static /32 for the peer's loopback via the transit IP (so the iBGP
  session can come up over a loopback source before any IGP).
- Runs ``router bgp 65100`` with one neighbor ``update-source lo``.
- Advertises its own loopback via ``network <lo>/32``.

Converged FIB on r1 contains, in the default VRF:

- 10.0.12.0/30 connected, eth1
- 10.0.0.1/32 connected, lo
- 10.0.0.2/32 bgp, next-hop 10.0.12.2 (learned from r2 via iBGP)
- (plus local /32 entries and the static peer-loopback /32 as the IGP substitute)
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


SPEC = TopologySpec(
    name="bgp-ibgp-2node",
    description="Two-router iBGP, loopback-sourced session over a /30 transit link.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        Node(
            name="r1",
            adapter=_frr,
            interfaces=(Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),),
            params={
                "asn": 65100,
                "loopback": "10.0.0.1",
                "peer": {
                    "name": "r2",
                    "loopback": "10.0.0.2",
                    "transit_ip": "10.0.12.2",
                },
                "enabled_daemons": ["bgpd", "staticd"],
            },
        ),
        Node(
            name="r2",
            adapter=_frr,
            interfaces=(Interface(name="eth1", ip="10.0.12.2/30", description="to r1"),),
            params={
                "asn": 65100,
                "loopback": "10.0.0.2",
                "peer": {
                    "name": "r1",
                    "loopback": "10.0.0.1",
                    "transit_ip": "10.0.12.1",
                },
                "enabled_daemons": ["bgpd", "staticd"],
            },
        ),
    ),
    links=(Link(a=("r1", "eth1"), b=("r2", "eth1")),),
)
