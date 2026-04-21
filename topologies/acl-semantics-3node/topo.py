"""acl-semantics-3node — flow-audit diff across FRR + cEOS + Batfish + Hammerhead.

Shape::

    r1 (FRR)  --- 10.0.12.0/30 --- r2 (cEOS) --- 10.0.23.0/30 --- r3 (FRR)
       lo 10.0.0.1/32                lo 10.0.0.2/32               lo 10.0.0.3/32

Every link is OSPFv2 point-to-point, single area 0. r2 carries a curated
overlapping permit/deny ACL bound ingress on ``Ethernet1`` (the r1-facing
interface) so packets arriving from r1 must clear the ACL before the FIB
decides whether to forward them on to r3.

Phase-8 benchmark intent: pick a small flow-set that exercises overlap
resolution (e.g. explicit ``deny tcp ... eq 22`` earlier in the list,
then a broad ``permit tcp ...``) and compare permit/deny verdicts across
three tools:

- vendor truth — cEOS ``show platform ... packet-tracer`` / rule-counter.
- Batfish      — ``bfq.testFilters(filter, headerSpace)``.
- Hammerhead   — ``hammerhead acl-audit --config <r2>``.

Gated behind ``bench --with-acl-semantics``: this topology only loads when
that flag is set, so a default bench run stays FRR-only and offline-test
friendly.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.ceos import CeosAdapter
from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_frr = FrrAdapter()
_ceos = CeosAdapter()
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_AREA = "0.0.0.0"
_ACL_NAME = "FLOW_AUDIT"
_FRR_ENABLED = ["ospfd", "staticd"]

# The ACL body is the same text pasted into both r2's cEOS startup-config
# (as ``ip access-list FLOW_AUDIT`` entries) and the packet-probe fixtures
# downstream tooling uses. Order matters: the early ``deny tcp any any eq 22``
# must beat the broad ``permit tcp any any`` below it, which is exactly the
# permit/deny overlap the phase-8 diff is designed to surface.
#
# Each entry is ``(seq, action, protocol, src, dst, l4)``; the cEOS template
# renders these into sequenced rule lines. Vendor+Batfish+Hammerhead must
# agree on the verdict for every probe in ``FLOW_PROBES`` below.
ACL_ENTRIES: tuple[tuple[int, str, str, str, str, str], ...] = (
    (10, "deny", "tcp", "any", "any", "eq 22"),
    (20, "permit", "icmp", "10.0.0.0/8", "10.0.0.0/8", ""),
    (30, "deny", "udp", "any", "any", "eq 53"),
    (40, "permit", "tcp", "10.0.0.0/8", "10.0.0.0/8", "eq 443"),
    (50, "permit", "tcp", "10.0.0.0/8", "10.0.0.0/8", "eq 80"),
    (60, "deny", "tcp", "any", "any", "range 6000 6100"),
    (70, "permit", "tcp", "any", "any", ""),
    (80, "deny", "ip", "any", "any", ""),
)

# Probe flows carried through every tool's flow-audit engine.
# Shape: ``{name, src, dst, protocol, src_port, dst_port, expected}``.
# ``expected`` is the verdict the first-match-wins ACL semantics dictate —
# it's what vendor truth SHOULD say, and what the diff fails loud on.
FLOW_PROBES: tuple[dict[str, str | int], ...] = (
    {
        "name": "ssh_deny_early",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "tcp",
        "src_port": 1024,
        "dst_port": 22,
        "expected": "deny",
    },
    {
        "name": "icmp_permit_intra_rfc1918",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "icmp",
        "src_port": 0,
        "dst_port": 0,
        "expected": "permit",
    },
    {
        "name": "dns_deny_by_proto",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "udp",
        "src_port": 40000,
        "dst_port": 53,
        "expected": "deny",
    },
    {
        "name": "https_permit_explicit",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "tcp",
        "src_port": 1024,
        "dst_port": 443,
        "expected": "permit",
    },
    {
        "name": "port_range_deny",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "tcp",
        "src_port": 1024,
        "dst_port": 6050,
        "expected": "deny",
    },
    {
        "name": "catch_all_permit",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "tcp",
        "src_port": 1024,
        "dst_port": 8080,
        "expected": "permit",
    },
    {
        "name": "implicit_deny_ip",
        "src": "10.0.0.1",
        "dst": "10.0.0.3",
        "protocol": "ospf",
        "src_port": 0,
        "dst_port": 0,
        "expected": "deny",
    },
)


def _frr_node(
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
            "enabled_daemons": _FRR_ENABLED,
            # r1/r3 don't bind the ACL — only r2 does — so the shared
            # frr.conf.j2 template guards on acl_bound_interface being truthy.
            "acl_bound_interface": None,
            "acl_name": _ACL_NAME,
            "acl_body": "",
        },
    )


def _ceos_node(
    name: str,
    loopback: str,
    interfaces: tuple[Interface, ...],
    acl_bound_interface: str,
) -> Node:
    return Node(
        name=name,
        adapter=_ceos,
        interfaces=interfaces,
        params={
            "loopback": loopback,
            "area": _AREA,
            "acl_name": _ACL_NAME,
            "acl_entries": ACL_ENTRIES,
            "acl_bound_interface": acl_bound_interface,
        },
    )


SPEC = TopologySpec(
    name="acl-semantics-3node",
    description=(
        "3-router OSPF triangle (linear); r2 is Arista cEOS carrying an "
        "overlapping permit/deny ACL bound ingress on the r1-facing iface."
    ),
    template_dir=_TEMPLATE_DIR,
    nodes=(
        _frr_node(
            "r1",
            "10.0.0.1",
            (Interface(name="eth1", ip="10.0.12.1/30", description="to r2"),),
        ),
        _ceos_node(
            "r2",
            "10.0.0.2",
            (
                Interface(name="Ethernet1", ip="10.0.12.2/30", description="to r1"),
                Interface(name="Ethernet2", ip="10.0.23.1/30", description="to r3"),
            ),
            acl_bound_interface="Ethernet1",
        ),
        _frr_node(
            "r3",
            "10.0.0.3",
            (Interface(name="eth1", ip="10.0.23.2/30", description="to r2"),),
        ),
    ),
    links=(
        Link(a=("r1", "eth1"), b=("r2", "Ethernet1")),
        Link(a=("r2", "Ethernet2"), b=("r3", "eth1")),
    ),
)
