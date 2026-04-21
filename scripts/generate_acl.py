#!/usr/bin/env python3
"""Generate the 500-entry overlapping ACL for the acl-heavy-parse topology.

The output is a deterministic FRR-flavor extended access-list with ~500 entries
of mixed permit/deny covering TCP, UDP, ICMP, host, range, and wildcard matches.
Running the same command twice produces byte-identical output — this is what
lets the benchmark assert "parser reads every entry" across vendor/Batfish/
Hammerhead without sampling.

Usage::

    python3 scripts/generate_acl.py --name HEAVY --entries 500 > acl.txt

Defaults match the topology consumer (name=HEAVY, entries=500).
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

_PROTOCOLS = ("tcp", "udp", "icmp")
_PORTS_EQ = (22, 23, 53, 80, 123, 161, 179, 443, 500, 1812, 3306, 5060, 8080, 8443, 9200)
_PORT_RANGES = ((1024, 2047), (3000, 3099), (5000, 5500), (6000, 6100), (8000, 8099))
_SRC_SUPERNETS = ("10.0.0.0", "10.1.0.0", "10.2.0.0", "172.16.0.0", "192.168.0.0")
_DST_SUPERNETS = ("10.100.0.0", "10.200.0.0", "172.17.0.0", "192.168.100.0", "203.0.113.0")


def _src_wild(idx: int) -> tuple[str, str]:
    base = _SRC_SUPERNETS[idx % len(_SRC_SUPERNETS)]
    octets = base.split(".")
    octets[2] = str((idx * 7) % 256)
    return ".".join(octets), "0.0.0.255"


def _dst_wild(idx: int) -> tuple[str, str]:
    base = _DST_SUPERNETS[idx % len(_DST_SUPERNETS)]
    octets = base.split(".")
    octets[2] = str((idx * 11) % 256)
    return ".".join(octets), "0.0.0.255"


def _line(idx: int) -> str:
    action = "permit" if idx % 3 != 0 else "deny"
    proto = _PROTOCOLS[idx % len(_PROTOCOLS)]
    src, src_mask = _src_wild(idx)
    dst, dst_mask = _dst_wild(idx)

    if proto == "icmp":
        # FRR/IOS extended ACLs allow bare icmp without L4 port.
        return f" {action} icmp {src} {src_mask} {dst} {dst_mask}"

    # tcp / udp → alternate eq, range, and "any port" (no L4 qualifier).
    kind = idx % 3
    if kind == 0:
        port = _PORTS_EQ[idx % len(_PORTS_EQ)]
        return f" {action} {proto} {src} {src_mask} {dst} {dst_mask} eq {port}"
    if kind == 1:
        lo, hi = _PORT_RANGES[idx % len(_PORT_RANGES)]
        return f" {action} {proto} {src} {src_mask} {dst} {dst_mask} range {lo} {hi}"
    return f" {action} {proto} {src} {src_mask} {dst} {dst_mask}"


def render(name: str, entries: int, out: TextIO) -> None:
    """Write an extended access-list named ``name`` with ``entries`` lines."""
    print(f"ip access-list extended {name}", file=out)
    for idx in range(entries):
        print(_line(idx), file=out)
    # Explicit trailing deny to make the "parser dropped the last line" class
    # of bugs loud — every tool must report this entry.
    print(" deny ip any any", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", default="HEAVY", help="ACL name (default: HEAVY)")
    parser.add_argument(
        "--entries",
        type=int,
        default=500,
        help="Number of generated entries (default: 500)",
    )
    args = parser.parse_args(argv)
    render(args.name, args.entries, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
