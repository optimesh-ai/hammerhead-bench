"""ospf-p2p-3node — three-router OSPFv2 single-area triangle over /30 P2P links.

Shape::

    r1 --- 10.0.12.0/30 --- r2
     \\                    /
      10.0.13.0/30  10.0.23.0/30
         \\               /
              r3

Every interface is ``ip ospf network point-to-point`` so there's no DR / BDR
election to muddy the FIB diff; next-hops resolve directly to the peer's
transit IP. All three routers redistribute their loopback into OSPF via
``network <lo>/32 area 0``.

The benchmark asks: does each of the three routers converge to
``<other_lo>/32 via <transit>``? Vendor, Batfish, and Hammerhead must agree
on every prefix, next-hop, and protocol code ("ospf" / "O").
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_AREA = "0.0.0.0"
_ENABLED = ["ospfd", "staticd"]


def _node(
    name: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "loopback": loopback,
            "area": _AREA,
            "enabled_daemons": _ENABLED,
            # P2P subnets are written directly from the interface IP in the
            # template, so no extra params are needed.
        },
    )


SPEC = TopologySpec(
    name="ospf-p2p-3node",
    description="Three-router OSPFv2 triangle, every link point-to-point.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _node(
            "r1",
            "10.0.0.1",
            (
                Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),
                Interface(name="eth2", ip="10.0.13.1/30", description="to r3"),
            ),
        ),
        _node(
            "r2",
            "10.0.0.2",
            (
                Interface(name="eth1", ip="10.0.12.2/30", description="to r1"),
                Interface(name="eth2", ip="10.0.23.1/30", description="to r3"),
            ),
        ),
        _node(
            "r3",
            "10.0.0.3",
            (
                Interface(name="eth1", ip="10.0.13.2/30", description="to r1"),
                Interface(name="eth2", ip="10.0.23.2/30", description="to r2"),
            ),
        ),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("r2", "eth1")),
        Link(a=("r1", "eth2"), b=("r3", "eth1")),
        Link(a=("r2", "eth2"), b=("r3", "eth2")),
    ),
)
