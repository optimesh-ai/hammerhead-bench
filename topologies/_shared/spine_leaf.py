"""Programmatic spine-leaf Clos builder for the large scale corpora.

Uses the same FRR eBGP pattern the hand-authored ``spine-leaf-6node``
fixture uses (spines share one AS, leaves each carry their own AS), but
sized up. The Jinja2 template is shared from that topology so there's
one authoritative rendering path.

Addressing plan (fully deterministic, no collisions up to the caps
below):

- spine loopback: ``10.0.0.100 + spine_index``         (spine_index 1..N)
- leaf loopback:  ``10.0.0.0 + leaf_index``            (leaf_index 1..M)
- transit /30s:   ``10.<spine_index>.<leaf_index>.0/30``
                  spine side = .1, leaf side = .2

Caps: ``spine_index <= 255``, ``leaf_index <= 255``. Beyond that the IP
plan breaks; the largest fixture in this bench stays well within.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.topology import Interface, Link, Node, TopologySpec

_FRR = FrrAdapter()

# One authoritative FRR template; re-uses the hand-authored spine-leaf-6node
# templates so there's a single source of truth for the rendering.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "spine-leaf-6node" / "templates"

# Spines share one AS, leaves each get their own. Matches spine-leaf-6node.
_SPINE_ASN = 65000
_LEAF_ASN_BASE = 65100


def build_spine_leaf_bgp(
    *,
    name: str,
    description: str,
    num_spines: int,
    num_leaves: int,
) -> TopologySpec:
    """Build a ``(num_spines × num_leaves)`` eBGP Clos topology.

    Every leaf uplinks to every spine. ``maximum-paths {num_spines}`` on
    each leaf so remote loopbacks install with N ECMP next-hops (one per
    spine), which is what the diff engine's next-hop set equality check
    asserts.
    """
    if num_spines < 1 or num_leaves < 1:
        raise ValueError("need at least one spine and one leaf")
    if num_spines > 255 or num_leaves > 255:
        raise ValueError("addressing plan only covers 1..255 on each axis")

    # Pre-compute per-leaf BGP peer lists (one entry per spine) and per-spine
    # BGP peer lists (one entry per leaf) so both sides of every session line
    # up on IP addresses exactly.
    spine_to_leaf_ifname: dict[int, list[str]] = {}
    leaf_to_spine_ifname: dict[int, list[str]] = {}

    spine_leaf_peers: dict[int, list[dict]] = {i: [] for i in range(1, num_spines + 1)}
    leaf_spine_peers: dict[int, list[dict]] = {i: [] for i in range(1, num_leaves + 1)}

    links: list[Link] = []

    for s in range(1, num_spines + 1):
        spine_to_leaf_ifname[s] = []
        for leaf_index in range(1, num_leaves + 1):
            spine_ip = f"10.{s}.{leaf_index}.1"
            leaf_ip = f"10.{s}.{leaf_index}.2"
            spine_iface = f"eth{leaf_index}"  # one iface per leaf on this spine
            leaf_iface = f"eth{s}"  # one iface per spine on this leaf
            spine_to_leaf_ifname[s].append(spine_iface)
            leaf_to_spine_ifname.setdefault(leaf_index, []).append(leaf_iface)

            spine_leaf_peers[s].append(
                {"asn": _LEAF_ASN_BASE + leaf_index, "ip": leaf_ip}
            )
            leaf_spine_peers[leaf_index].append(
                {"asn": _SPINE_ASN, "ip": spine_ip}
            )

            links.append(
                Link(
                    a=(f"s{s}", spine_iface),
                    b=(f"l{leaf_index}", leaf_iface),
                )
            )

    nodes: list[Node] = []
    for s in range(1, num_spines + 1):
        interfaces = tuple(
            Interface(
                name=f"eth{leaf_index}",
                ip=f"10.{s}.{leaf_index}.1/30",
                description=f"to l{leaf_index}",
            )
            for leaf_index in range(1, num_leaves + 1)
        )
        nodes.append(
            Node(
                name=f"s{s}",
                adapter=_FRR,
                interfaces=interfaces,
                params={
                    "role": "spine",
                    "asn": _SPINE_ASN,
                    "loopback": f"10.0.0.{100 + s}",
                    "leaf_peers": spine_leaf_peers[s],
                    "enabled_daemons": ["bgpd", "staticd"],
                    "ecmp_paths": num_spines,
                },
            )
        )

    for leaf_index in range(1, num_leaves + 1):
        interfaces = tuple(
            Interface(
                name=f"eth{s}",
                ip=f"10.{s}.{leaf_index}.2/30",
                description=f"to s{s}",
            )
            for s in range(1, num_spines + 1)
        )
        nodes.append(
            Node(
                name=f"l{leaf_index}",
                adapter=_FRR,
                interfaces=interfaces,
                params={
                    "role": "leaf",
                    "asn": _LEAF_ASN_BASE + leaf_index,
                    "loopback": f"10.0.0.{leaf_index}",
                    "spine_peers": leaf_spine_peers[leaf_index],
                    "enabled_daemons": ["bgpd", "staticd"],
                    "ecmp_paths": num_spines,
                },
            )
        )

    return TopologySpec(
        name=name,
        description=description,
        template_dir=_TEMPLATE_DIR,
        nodes=tuple(nodes),
        links=tuple(links),
    )
