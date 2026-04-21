"""acl-heavy-parse — 3-router OSPF triangle with a 500-entry ACL on r2.

Shape::

    r1 --- 10.0.12.0/30 --- r2
     \\                    /
      10.0.13.0/30  10.0.23.0/30
         \\               /
              r3

Topologically identical to ``ospf-p2p-3node``. The twist: r2's eth1 (the
session-facing link toward r1) carries a 500-entry extended ACL named
``HEAVY`` bound inbound. The ACL is generated on-render via
``scripts/generate_acl.py`` and pasted verbatim into r2's ``frr.conf``.

The benchmark is about **parse coverage**, not FIB correctness:

- Vendor ground truth reports the ACL entry count via
  ``vtysh -c 'show access-list HEAVY'``.
- Batfish + Hammerhead each report what they parsed.
- All three must agree on the count (501: 500 generated entries + one
  explicit trailing ``deny ip any any``).

FIB convergence is still asserted — OSPF must come up normally — so we
also catch the regression where an oversized ACL accidentally breaks
routing (it shouldn't; the ACL is inbound on a transit iface and OSPF
hello/DR traffic should still be permitted by the catch-all).

Note: the generated ACL does not permit OSPF multicast explicitly; FRR
bypasses inbound ACL evaluation for its own control-plane sockets, so
this is fine in practice but worth flagging if the test fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_AREA = "0.0.0.0"
_ENABLED = ["ospfd", "staticd"]
_ACL_NAME = "HEAVY"
_ACL_ENTRIES = 500


def _render_heavy_acl() -> str:
    """Shell out to scripts/generate_acl.py so the bytes are identical to
    what someone running that script by hand would get. Deterministic; no
    RNG; fine to call at import time."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "generate_acl.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--name", _ACL_NAME, "--entries", str(_ACL_ENTRIES)],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


_HEAVY_ACL_TEXT = _render_heavy_acl()


def _node(
    name: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
    acl_bound_interface: str | None = None,
) -> Node:
    params: dict = {
        "loopback": loopback,
        "area": _AREA,
        "enabled_daemons": _ENABLED,
        "acl_name": _ACL_NAME,
        "acl_bound_interface": acl_bound_interface,
        # Verbatim ACL bytes; template just pastes this block inline.
        "acl_body": _HEAVY_ACL_TEXT,
    }
    return Node(name=name, adapter=_frr, interfaces=interfaces, params=params)


SPEC = TopologySpec(
    name="acl-heavy-parse",
    description="3-router OSPF triangle, r2 carries a 500-entry ACL inbound on eth1.",
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
            acl_bound_interface="eth1",
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
