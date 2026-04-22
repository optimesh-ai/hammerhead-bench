"""Fat-tree(k) config generator — backs the k=64 scale fixture.

Emits one config per switch into a caller-supplied ``configs_dir``:

- core + aggregation switches → ``<host>.cfg`` (Arista EOS)
- edge (ToR) switches          → ``<host>/frr.conf`` (FRR 8.5, subdir)

All switches run OSPFv2 area 0 in a single-area DC underlay — no BGP, no
route-maps, no redistribution. Matches the Hammerhead main-repo
``tools/benchmarks/fat_tree.py`` generator (same layout, same addressing
scheme) so head-to-head numbers are directly comparable across repos.

Fat-tree(k) shape:
    core layer  : (k/2)^2 switches
    agg layer   : k pods × k/2 switches per pod
    edge layer  : k pods × k/2 switches per pod
    total       : 5k²/4 switches
    k=64 → 1,024 core + 2,048 agg + 2,048 edge = **5,120** switches

Addressing:
    loopbacks : 10.0.0.0/12   → first n_total free addresses
    p2p /30s  : 10.128.0.0/10 → one /30 per link (plenty of headroom)

The generator is deterministic; regenerating always produces byte-identical
output so re-runs are reproducible for diff-hashing.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

__all__ = ["generate_fat_tree"]

_LOOPBACK_BASE = ipaddress.IPv4Network("10.0.0.0/12")
_P2P_BASE = ipaddress.IPv4Network("10.128.0.0/10")


class _SubnetAllocator:
    def __init__(self, base: ipaddress.IPv4Network, new_prefix: int = 30) -> None:
        self._it = base.subnets(new_prefix=new_prefix)

    def next_p2p(self) -> tuple[str, str, str]:
        subnet = next(self._it)
        hosts = list(subnet.hosts())
        return str(hosts[0]), str(hosts[1]), str(subnet.with_prefixlen)


class _LoopbackAllocator:
    def __init__(self, base: ipaddress.IPv4Network) -> None:
        self._i = int(base.network_address) + 1

    def next(self) -> str:
        addr = ipaddress.IPv4Address(self._i)
        self._i += 1
        return str(addr)


def generate_fat_tree(k: int, configs_dir: Path) -> None:
    """Emit a fat-tree(k) corpus into ``configs_dir`` (created if missing).

    ``k`` must be even and at least 4. ``configs_dir`` is overwritten
    file-by-file; previous configs for the same topology stay on disk
    unless explicitly cleaned (callers that want a clean regen should
    ``shutil.rmtree`` first — our sim-only pipeline always works in a
    fresh ``tempfile.TemporaryDirectory`` so this is never an issue in
    practice).
    """
    if k % 2 != 0 or k < 4:
        raise ValueError(f"fat-tree k must be even and >= 4; got {k}")

    configs_dir.mkdir(parents=True, exist_ok=True)
    half = k // 2
    n_core = half * half
    n_agg = k * half
    n_edge = k * half

    lo_alloc = _LoopbackAllocator(_LOOPBACK_BASE)
    p2p = _SubnetAllocator(_P2P_BASE)

    core_lo = [lo_alloc.next() for _ in range(n_core)]
    agg_lo = [lo_alloc.next() for _ in range(n_agg)]
    edge_lo = [lo_alloc.next() for _ in range(n_edge)]

    core_links: dict[int, list[tuple[str, str]]] = {i: [] for i in range(n_core)}
    agg_links: dict[int, list[tuple[str, str]]] = {i: [] for i in range(n_agg)}
    edge_links: dict[int, list[tuple[str, str]]] = {i: [] for i in range(n_edge)}

    # core ↔ agg: (k pods) × (half per pod) = k*half = n_agg links
    # Each pod p's agg[g] connects to one core — c_idx = g*half + pod%half
    for pod in range(k):
        for g in range(half):
            agg_idx = pod * half + g
            c_idx = g * half + (pod % half)
            ha, hb, subnet = p2p.next_p2p()
            core_links[c_idx].append((ha, subnet))
            agg_links[agg_idx].append((hb, subnet))

    # agg ↔ edge: each agg in a pod connects to every edge in that pod
    for pod in range(k):
        for a in range(half):
            agg_idx = pod * half + a
            for e in range(half):
                edge_idx = pod * half + e
                ha, hb, subnet = p2p.next_p2p()
                agg_links[agg_idx].append((ha, subnet))
                edge_links[edge_idx].append((hb, subnet))

    for i in range(n_core):
        hostname = f"core-{i + 1:04d}"
        (configs_dir / f"{hostname}.cfg").write_text(
            _eos_config(hostname, core_lo[i], core_links[i])
        )

    for i in range(n_agg):
        pod = i // half
        slot = i % half
        hostname = f"agg-pod{pod + 1:02d}-{slot + 1:02d}"
        (configs_dir / f"{hostname}.cfg").write_text(
            _eos_config(hostname, agg_lo[i], agg_links[i])
        )

    for i in range(n_edge):
        pod = i // half
        slot = i % half
        hostname = f"edge-pod{pod + 1:02d}-{slot + 1:02d}"
        d = configs_dir / hostname
        d.mkdir(exist_ok=True)
        (d / "frr.conf").write_text(_frr_config(hostname, edge_lo[i], edge_links[i]))


def _eos_config(hostname: str, lo_ip: str, links: list[tuple[str, str]]) -> str:
    """EOS config for core + agg switches. Single-area OSPFv2 underlay."""

    def wc(pfx: str) -> str:
        return str(ipaddress.IPv4Network(pfx, strict=False).hostmask)

    def net(pfx: str) -> str:
        return str(ipaddress.IPv4Network(pfx, strict=False).network_address)

    lines = [
        "!RANCID-CONTENT-TYPE: arista",
        "!",
        f"hostname {hostname}",
        "!",
        "ip routing",
        "!",
        "interface Loopback0",
        "   description router-id loopback",
        f"   ip address {lo_ip}/32",
        "!",
    ]
    ospf_nets: list[tuple[str, str]] = [(f"{lo_ip}/32", "0.0.0.0")]
    for eth_idx, (my_ip, subnet) in enumerate(links, 1):
        n = ipaddress.IPv4Network(subnet, strict=False)
        lines += [
            f"interface Ethernet{eth_idx}",
            "   no switchport",
            f"   ip address {my_ip}/30",
            "   ip ospf network point-to-point",
            "   ip ospf cost 1",
            "   no shutdown",
            "!",
        ]
        ospf_nets.append((n.with_prefixlen, "0.0.0.0"))
    lines.append("router ospf 1")
    lines.append(f"   router-id {lo_ip}")
    for pfx, area in ospf_nets:
        lines.append(f"   network {net(pfx)} {wc(pfx)} area {area}")
    lines.append("   passive-interface Loopback0")
    lines.append("   max-lsa 20000")
    lines.append("!")
    lines.append("end")
    return "\n".join(lines) + "\n"


def _frr_config(hostname: str, lo_ip: str, links: list[tuple[str, str]]) -> str:
    """FRR config for edge (ToR) switches. OSPFv2 area 0."""
    lines = [
        "frr version 8.5",
        "frr defaults traditional",
        f"hostname {hostname}",
        "no ipv6 forwarding",
        "!",
        "interface lo",
        f" ip address {lo_ip}/32",
        "!",
    ]
    subnets: list[str] = []
    for eth_idx, (my_ip, subnet) in enumerate(links, 1):
        n = ipaddress.IPv4Network(subnet, strict=False)
        lines += [
            f"interface eth{eth_idx}",
            " description uplink",
            f" ip address {my_ip}/30",
            " ip ospf cost 1",
            " ip ospf hello-interval 10",
            " ip ospf dead-interval 40",
            "!",
        ]
        subnets.append(n.with_prefixlen)
    lines += [
        "router ospf",
        f" ospf router-id {lo_ip}",
        f" network {lo_ip}/32 area 0",
    ]
    for s in subnets:
        lines.append(f" network {s} area 0")
    lines += [" passive-interface lo", "!", "line vty", "!", "end"]
    return "\n".join(lines) + "\n"
