"""isis-l1l2-4node — four-router IS-IS with L1/L2 hierarchy and wide metrics.

Shape::

    r1 (L1)   --   r2 (L1L2)   --   r3 (L1L2)   --   r4 (L1)
    area 49.0001   area 49.0001     area 49.0002     area 49.0002

r1 and r4 are L1-only; r2 and r3 are L1L2 and form the L2 backbone. Links
are /30 P2P veth pairs. All interfaces run wide-metric (RFC 5305) TLV 22.

Convergence expectation:

- r1 sees r2 via L1 adjacency, gets a default route injected from r2 (ATT
  bit). r1's FIB to r3's or r4's loopback resolves via the injected
  default -> r2.
- r2 and r3 share an L2 adjacency. r2 sees all four loopbacks (r1 via L1,
  r3 via L2, r4 via L2-then-L1).
- L1-preferred tiebreaker (RFC 1195): when r2 has both an L1 route (inside
  its own area) and an L2 route (via the backbone), it prefers the L1 one.

Benchmarks:

- Every router's FIB matches across vendor, Batfish, Hammerhead.
- The ATT-default on r1/r4 is present on exactly those two routers.
- No narrow-metric TLVs anywhere (all three outputs report wide).
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_ENABLED = ["isisd", "staticd"]


def _node(
    name: str,
    net: str,
    is_type: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
) -> Node:
    return Node(
        name=name,
        adapter=_frr,
        interfaces=interfaces,
        params={
            "loopback": loopback,
            "net": net,
            "is_type": is_type,
            "enabled_daemons": _ENABLED,
        },
    )


SPEC = TopologySpec(
    name="isis-l1l2-4node",
    description="Four-router IS-IS with L1/L2 hierarchy across two areas.",
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _node(
            "r1",
            net="49.0001.0000.0000.0001.00",
            is_type="level-1",
            loopback="10.0.0.1",
            interfaces=(
                Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),
            ),
        ),
        _node(
            "r2",
            net="49.0001.0000.0000.0002.00",
            is_type="level-1-2",
            loopback="10.0.0.2",
            interfaces=(
                Interface(name="eth1", ip="10.0.12.2/30", description="to r1 (L1)"),
                Interface(name="eth2", ip="10.0.23.1/30", description="to r3 (L2 backbone)"),
            ),
        ),
        _node(
            "r3",
            net="49.0002.0000.0000.0003.00",
            is_type="level-1-2",
            loopback="10.0.0.3",
            interfaces=(
                Interface(name="eth1", ip="10.0.23.2/30", description="to r2 (L2 backbone)"),
                Interface(name="eth2", ip="10.0.34.1/30", description="to r4 (L1)"),
            ),
        ),
        _node(
            "r4",
            net="49.0002.0000.0000.0004.00",
            is_type="level-1",
            loopback="10.0.0.4",
            interfaces=(
                Interface(name="eth1", ip="10.0.34.2/30", description="to r3"),
            ),
        ),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("r2", "eth1")),
        Link(a=("r2", "eth2"), b=("r3", "eth1")),
        Link(a=("r3", "eth2"), b=("r4", "eth1")),
    ),
)
