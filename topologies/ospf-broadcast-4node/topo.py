"""ospf-broadcast-4node — four-router OSPFv2 broadcast segment with DR/BDR.

Shape: all four routers share one broadcast L2 segment via a clab ``bridge``
node (``hub``)::

    r1 --\\
    r2 ---+-- hub(bridge) -- 10.0.99.0/24, OSPF area 0
    r3 ---|
    r4 --/

The four routers all bring up ``eth1`` on the same /24 and run OSPF with
``ip ospf network broadcast``. Priority knobs are set so r4 (priority 255)
deterministically wins DR and r3 (priority 100) wins BDR. r1 and r2 carry
priority 0 so they can never win — adding a second priority tie on the
other side would make the benchmark non-deterministic.

Benchmarks:

- DR is r4, BDR is r3 in both simulators.
- Every router learns the loopbacks of the three others via Type-2
  network LSA, next-hop equals the advertising router's interface IP
  on the shared segment (NOT the DR's IP).
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.bridge import BridgeAdapter
from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_bridge = BridgeAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_AREA = "0.0.0.0"
_SUBNET = "10.0.99.0/24"


def _router(name: str, ip: str, router_id: str, priority: int) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=(Interface(name="eth1", ip=f"{ip}/24", description="broadcast segment"),),
        params={
            "loopback": router_id,
            "segment_ip": ip,
            "segment_cidr": _SUBNET,
            "area": _AREA,
            "ospf_priority": priority,
            "enabled_daemons": ["ospfd", "staticd"],
        },
    )


SPEC = TopologySpec(
    name="ospf-broadcast-4node",
    description="Four routers on one OSPFv2 broadcast segment with DR/BDR election.",
    template_dir=_TEMPLATE_DIR,
    # r4 (priority 255) wins DR; r3 (priority 100) wins BDR; r1/r2 are
    # non-eligible so the election is deterministic.
    nodes=(
        _router("r1", "10.0.99.1", "10.0.0.1", priority=0),
        _router("r2", "10.0.99.2", "10.0.0.2", priority=0),
        _router("r3", "10.0.99.3", "10.0.0.3", priority=100),
        _router("r4", "10.0.99.4", "10.0.0.4", priority=255),
        Node(name="hub", adapter=_bridge, interfaces=(), params={}),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("hub", "eth1")),
        Link(a=("r2", "eth1"), b=("hub", "eth2")),
        Link(a=("r3", "eth1"), b=("hub", "eth3")),
        Link(a=("r4", "eth1"), b=("hub", "eth4")),
    ),
)
