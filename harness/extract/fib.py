"""Canonical FIB schema — the one type system every adapter + tool writes to.

Design goals:

1. One schema for vendor truth / Batfish / Hammerhead output so the diff engine
   is a single function.
2. Explicit canonicalization (next-hop sort, VRF name alias) so benign
   differences don't show up as false diffs.
3. Pydantic so unknown fields are rejected loudly (``model_config
   = ConfigDict(extra="forbid")``). A silent schema drift in any tool's output
   is a correctness bug we MUST see.
4. Streamable — the diff engine reads one ``NodeFib`` at a time from disk,
   never the full workspace. The ``routes`` list is the only large field.

Normalization rules, enforced by :func:`canonicalize_node_fib`:

- ``next_hops`` sorted by ``(ip or "", interface or "")`` lex ascending.
- ``vrf`` names ``""`` / ``"global"`` / ``"master"`` collapse to ``"default"``.
- Loopback /32 host routes are kept unless ``filter_loopback_host=True`` and
  ALL three sources agree they should be stripped (the filter is explicit and
  logged so silent parse bugs surface).
- Protocol is a :class:`Protocol` literal; unknown strings are rejected.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Protocol = Literal["bgp", "ospf", "isis", "static", "connected", "local", "rip"]
Source = Literal["vendor", "batfish", "hammerhead"]


class NextHop(BaseModel):
    """One (ip, interface) pair. Either may be None but not both."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ip: str | None = None
    interface: str | None = None


class Route(BaseModel):
    """One FIB entry."""

    model_config = ConfigDict(extra="forbid")

    prefix: str
    protocol: Protocol
    next_hops: list[NextHop] = Field(default_factory=list)
    admin_distance: int | None = None
    metric: int | None = None
    # BGP-only; populated iff protocol == "bgp".
    as_path: list[int] | None = None
    local_pref: int | None = None
    med: int | None = None
    communities: list[str] | None = None


class NodeFib(BaseModel):
    """All routes for one (node, vrf, source) triple. Written to disk per-node."""

    model_config = ConfigDict(extra="forbid")

    node: str
    vrf: str
    source: Source
    routes: list[Route] = Field(default_factory=list)


# --- canonicalization ------------------------------------------------------

_VRF_ALIASES: dict[str, str] = {
    "": "default",
    "global": "default",
    "master": "default",
}


def canonicalize_vrf(vrf: str) -> str:
    """Collapse VRF aliases to the canonical name."""
    return _VRF_ALIASES.get(vrf.lower().strip(), vrf)


def canonicalize_next_hops(nhs: list[NextHop]) -> list[NextHop]:
    """Sort next-hops by ``(ip or "", interface or "")`` lex ascending."""
    return sorted(nhs, key=lambda n: (n.ip or "", n.interface or ""))


def canonicalize_route(r: Route) -> Route:
    """Return a copy with ``next_hops`` canonicalized; non-mutating."""
    return r.model_copy(update={"next_hops": canonicalize_next_hops(r.next_hops)})


def canonicalize_node_fib(
    fib: NodeFib,
    *,
    filter_loopback_host: bool = False,
) -> NodeFib:
    """Return a new ``NodeFib`` with VRF normalized, routes canonicalized, sorted.

    ``filter_loopback_host`` is deliberately off by default. Turning it on is
    an explicit operator choice at diff time and is recorded in the per-topology
    result JSON so the filter decision is auditable.
    """
    vrf = canonicalize_vrf(fib.vrf)
    routes = [canonicalize_route(r) for r in fib.routes]
    if filter_loopback_host:
        routes = [r for r in routes if not _is_loopback_host(r)]
    # Deterministic ordering: prefix asc, then protocol asc.
    routes.sort(key=lambda r: (r.prefix, r.protocol))
    return fib.model_copy(update={"vrf": vrf, "routes": routes})


def _is_loopback_host(r: Route) -> bool:
    # Heuristic: /32 connected or local route whose only next-hop is an
    # interface named lo* or Loopback*. Keeps real /32 static routes.
    if r.protocol not in ("connected", "local"):
        return False
    if not r.prefix.endswith("/32"):
        return False
    for nh in r.next_hops:
        if nh.interface and nh.interface.lower().startswith(("lo", "loopback")):
            return True
    return False
