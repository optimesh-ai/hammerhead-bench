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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.aggregate import LoopbackPolicy

Protocol = Literal["bgp", "ospf", "isis", "static", "connected", "local", "rip"]
Source = Literal["vendor", "batfish", "hammerhead"]

# FRR's `show ip route json` exposes a handful of protocols we don't model.
# Keep the map explicit so schema drift (new FRR version adds a protocol)
# surfaces as a ValueError instead of silently dropping routes.
_FRR_PROTOCOL_MAP: dict[str, Protocol] = {
    "bgp": "bgp",
    "ospf": "ospf",
    "isis": "isis",
    "static": "static",
    "connected": "connected",
    "local": "local",
    "rip": "rip",
}
_FRR_PROTOCOL_SKIP: frozenset[str] = frozenset(
    {
        "kernel",  # routes from the host kernel (eth0 mgmt, docker bridge)
        "table",  # non-main kernel table imports
        "system",  # clab-internal plumbing
        "nhrp",  # not benchmarked
        "babel",  # not benchmarked
    }
)


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
    loopback_policy: LoopbackPolicy | None = None,
) -> NodeFib:
    """Return a new ``NodeFib`` with VRF normalized, routes canonicalized, sorted.

    Loopback handling is controlled by either:

    * :class:`LoopbackPolicy` (preferred — symmetric, three-valued). When
      ``loopback_policy`` is supplied it wins over ``filter_loopback_host``.
    * Legacy ``filter_loopback_host: bool`` — back-compat bridge, maps to
      ``STRIP`` when True and ``PASSTHROUGH`` when False via
      :meth:`LoopbackPolicy.from_bool`.

    Semantic of each policy at the canonicalizer surface:

    * ``STRIP`` — drop every ``lo*``-interface /32 connected/local entry.
      Applied symmetrically to vendor / Batfish / Hammerhead so the diff
      engine reports only routes the *control plane* originated.
    * ``MATERIALIZE`` — keep every route the adapter produced (diagnostic
      view; synthesis of the matching sim-side /32 host entries is a
      higher-level concern and not done here).
    * ``PASSTHROUGH`` — identical to ``MATERIALIZE`` at this layer; left
      distinct so upstream code can record which policy was requested.

    The policy decision is passed through in the result and is the auditable
    record in per-topology bench output.
    """
    policy = loopback_policy or LoopbackPolicy.from_bool(filter_loopback_host)
    vrf = canonicalize_vrf(fib.vrf)
    routes = [canonicalize_route(r) for r in fib.routes]
    if policy.strip_loopback_host:
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


# --- FRR JSON parsing ------------------------------------------------------


def parse_frr_route_json(
    data: dict[str, Any],
    *,
    node_name: str,
    source: Source = "vendor",
) -> list[NodeFib]:
    """Convert FRR's ``show ip route vrf all json`` output to canonical NodeFibs.

    Returns one ``NodeFib`` per VRF. Only entries with ``selected=True`` AND
    ``installed=True`` are kept (those are what zebra actually programmed into
    the kernel FIB). Protocols in :data:`_FRR_PROTOCOL_SKIP` are dropped
    silently; unknown protocols raise ``ValueError`` so FRR version drift
    surfaces loudly.

    FRR emits two slightly different shapes:

    - single-VRF flat:  ``{"<prefix>": [<entries>], ...}``
    - multi-VRF nested: ``{"<vrf>": {"<prefix>": [<entries>], ...}, ...}``

    Detected by inspecting the first value: a list means flat, a dict means
    nested.
    """
    if not data:
        return [NodeFib(node=node_name, vrf="default", source=source, routes=[])]

    first_val = next(iter(data.values()))
    is_nested = isinstance(first_val, dict)
    vrf_map: dict[str, dict[str, list[dict[str, Any]]]] = (
        data if is_nested else {"default": data}  # type: ignore[dict-item]
    )

    results: list[NodeFib] = []
    for vrf_name, prefix_map in vrf_map.items():
        routes: list[Route] = []
        for prefix, entries in prefix_map.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                parsed = _parse_frr_route_entry(prefix, entry)
                if parsed is not None:
                    routes.append(parsed)
        results.append(
            NodeFib(node=node_name, vrf=canonicalize_vrf(vrf_name), source=source, routes=routes)
        )
    return results


def _parse_frr_route_entry(prefix: str, entry: dict[str, Any]) -> Route | None:
    """Convert one FRR route entry to a ``Route`` or return None to skip it.

    Skip rules:
    - ``selected`` is False or missing.
    - ``installed`` is False (entry lost the best-path race, or not yet in FIB).
    - Protocol is in :data:`_FRR_PROTOCOL_SKIP`.

    Raises ``ValueError`` if the protocol is unknown (neither mapped nor
    skipped) so FRR version drift surfaces loudly.
    """
    if not entry.get("selected") or not entry.get("installed"):
        return None
    raw_proto = (entry.get("protocol") or "").lower()
    if raw_proto in _FRR_PROTOCOL_SKIP:
        return None
    protocol = _FRR_PROTOCOL_MAP.get(raw_proto)
    if protocol is None:
        raise ValueError(
            f"unknown FRR protocol {raw_proto!r} for prefix {prefix!r}; "
            "update _FRR_PROTOCOL_MAP or _FRR_PROTOCOL_SKIP"
        )
    nhs: list[NextHop] = []
    for nh in entry.get("nexthops", []):
        if not isinstance(nh, dict):
            continue
        if nh.get("active") is False:
            continue
        ip = nh.get("ip")
        iface = nh.get("interfaceName")
        if ip is None and iface is None:
            continue
        nhs.append(NextHop(ip=ip, interface=iface))
    return Route(
        prefix=prefix,
        protocol=protocol,
        next_hops=nhs,
        admin_distance=entry.get("distance"),
        metric=entry.get("metric"),
    )


# --- EOS JSON parsing ------------------------------------------------------

# Arista EOS route protocol strings from ``show ip route vrf all | json``.
# EOS breaks OSPF and IS-IS into sub-protocols by LSA class; we collapse them
# back to the parent protocol for cross-vendor comparability. Unknown strings
# raise ValueError so EOS version drift surfaces loudly (same policy as FRR).
_EOS_PROTOCOL_MAP: dict[str, Protocol] = {
    "bgp": "bgp",
    "ibgp": "bgp",
    "ebgp": "bgp",
    "ospf": "ospf",
    "ospf intra area": "ospf",
    "ospf inter area": "ospf",
    "ospf external type 1": "ospf",
    "ospf external type 2": "ospf",
    "ospf nssa external type 1": "ospf",
    "ospf nssa external type 2": "ospf",
    "ospfintraarea": "ospf",
    "ospfinterarea": "ospf",
    "ospfexternal": "ospf",
    "isis": "isis",
    "isis level-1": "isis",
    "isis level-2": "isis",
    "static": "static",
    "connected": "connected",
    "direct": "connected",  # EOS labels interface /32s "direct" in some versions
    "local": "local",
    "rip": "rip",
}
_EOS_PROTOCOL_SKIP: frozenset[str] = frozenset(
    {
        # Management / kernel / internal routes that never factor into the
        # benchmark. Keep explicit so EOS version drift can't silently grow
        # the set.
        "attached host",
        "attached-host",
        "aggregate",
        "dhcp",
        "vxlan control service",
    }
)


def parse_eos_route_json(
    data: dict[str, Any],
    *,
    node_name: str,
    source: Source = "vendor",
) -> list[NodeFib]:
    """Convert EOS's ``show ip route vrf all | json`` output to canonical NodeFibs.

    EOS schema (multi-VRF native)::

        {"vrfs": {"<vrf>": {"routes": {"<prefix>": {
            "routeType": "ospfIntraArea",
            "routeAction": "forward",
            "kernelProgrammed": True,
            "directlyConnected": False,
            "preference": 110,
            "metric": 20,
            "vias": [{"interface": "Ethernet1", "nexthopAddr": "10.0.12.1"}],
            "protocol": "ospf intra area",
        }}}}}

    Filters:
    - ``routeAction`` must be ``forward`` (not ``drop``/``discard``).
    - ``kernelProgrammed`` must be True (entry reached the FIB).

    Routes with an empty ``vrfs`` block emit one empty ``default`` NodeFib so
    the downstream pipeline always has a per-node file to write.
    """
    if not data or not isinstance(data, dict):
        return [NodeFib(node=node_name, vrf="default", source=source, routes=[])]

    vrfs = data.get("vrfs") if isinstance(data.get("vrfs"), dict) else None
    if vrfs is None:
        return [NodeFib(node=node_name, vrf="default", source=source, routes=[])]

    results: list[NodeFib] = []
    for vrf_name, vrf_body in vrfs.items():
        if not isinstance(vrf_body, dict):
            continue
        routes: list[Route] = []
        route_map = vrf_body.get("routes", {})
        if not isinstance(route_map, dict):
            route_map = {}
        for prefix, entry in route_map.items():
            if not isinstance(entry, dict):
                continue
            parsed = _parse_eos_route_entry(prefix, entry)
            if parsed is not None:
                routes.append(parsed)
        results.append(
            NodeFib(
                node=node_name,
                vrf=canonicalize_vrf(vrf_name),
                source=source,
                routes=routes,
            )
        )
    # EOS can emit `{"vrfs": {}}` for a freshly-booted node. Keep the downstream
    # invariant that every node has at least a default NodeFib written.
    if not results:
        results.append(NodeFib(node=node_name, vrf="default", source=source, routes=[]))
    return results


def _parse_eos_route_entry(prefix: str, entry: dict[str, Any]) -> Route | None:
    """Convert one EOS route entry to a ``Route`` or return None to skip it."""
    action = (entry.get("routeAction") or "forward").lower()
    if action not in ("forward", "permit"):
        return None
    if entry.get("kernelProgrammed") is False:
        return None
    raw_proto = (entry.get("protocol") or "").lower().strip()
    if not raw_proto:
        # Some EOS versions put the label only in routeType.
        raw_proto = _route_type_to_proto(entry.get("routeType") or "")
    if raw_proto in _EOS_PROTOCOL_SKIP:
        return None
    protocol = _EOS_PROTOCOL_MAP.get(raw_proto)
    if protocol is None:
        raise ValueError(
            f"unknown EOS protocol {raw_proto!r} for prefix {prefix!r}; "
            "update _EOS_PROTOCOL_MAP or _EOS_PROTOCOL_SKIP"
        )
    nhs: list[NextHop] = []
    for via in entry.get("vias", []):
        if not isinstance(via, dict):
            continue
        ip = via.get("nexthopAddr") or via.get("nextHopAddr")
        iface = via.get("interface")
        if not ip and not iface:
            continue
        # EOS uses "0.0.0.0" for connected routes with no explicit next-hop;
        # drop those to match FRR's representation (interface-only).
        if ip == "0.0.0.0":
            ip = None
        nhs.append(NextHop(ip=ip, interface=iface))
    return Route(
        prefix=prefix,
        protocol=protocol,
        next_hops=nhs,
        admin_distance=entry.get("preference"),
        metric=entry.get("metric"),
    )


def _route_type_to_proto(route_type: str) -> str:
    """Map EOS ``routeType`` camelCase tokens to the lowercased protocol label.

    EOS routeType examples: ``ospfIntraArea``, ``ospfInterArea``, ``bgp``,
    ``static``, ``connected``, ``isisLevel1``, ``isisLevel2``.
    """
    rt = route_type.strip()
    if not rt:
        return ""
    mapping = {
        "ospfIntraArea": "ospf intra area",
        "ospfInterArea": "ospf inter area",
        "ospfExternalType1": "ospf external type 1",
        "ospfExternalType2": "ospf external type 2",
        "ospfNssaExternalType1": "ospf nssa external type 1",
        "ospfNssaExternalType2": "ospf nssa external type 2",
        "isisLevel1": "isis level-1",
        "isisLevel2": "isis level-2",
        "bgpInternal": "ibgp",
        "bgpExternal": "ebgp",
    }
    return mapping.get(rt, rt.lower())


def merge_bgp_attributes(fib: NodeFib, bgp_json: dict[str, Any]) -> NodeFib:
    """Return a new ``NodeFib`` with AS_PATH / LOCAL_PREF / MED populated on BGP routes.

    ``bgp_json`` is the raw ``show ip bgp vrf all json`` output. Each VRF-scoped
    block has a ``routes`` dict of ``{prefix: [path_info, ...]}`` where the path
    marked ``bestpath: true`` is what ended up in the FIB.

    Non-BGP routes and BGP routes without a matching entry are passed through
    unchanged. Communities are not populated in phase 2 (requires a follow-up
    ``show ip bgp <prefix> json`` per route to get the full attribute set).
    """
    # Collapse bgp_json into a per-(vrf, prefix) -> best-path dict.
    best: dict[tuple[str, str], dict[str, Any]] = {}
    blocks = _walk_bgp_blocks(bgp_json)
    for vrf_name, block in blocks:
        vrf = canonicalize_vrf(vrf_name)
        for prefix, paths in block.get("routes", {}).items():
            if not isinstance(paths, list):
                continue
            for p in paths:
                if p.get("bestpath") is True:
                    best[(vrf, prefix)] = p
                    break

    target_vrf = canonicalize_vrf(fib.vrf)
    updated: list[Route] = []
    for r in fib.routes:
        if r.protocol != "bgp":
            updated.append(r)
            continue
        pi = best.get((target_vrf, r.prefix))
        if pi is None:
            updated.append(r)
            continue
        updated.append(
            r.model_copy(
                update={
                    "as_path": _parse_as_path(pi.get("path")),
                    "local_pref": pi.get("locPrf"),
                    "med": pi.get("metric"),
                }
            )
        )
    return fib.model_copy(update={"routes": updated})


def _walk_bgp_blocks(bgp_json: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(vrf_name, block)`` pairs from ``show ip bgp vrf all json``.

    FRR emits either a single-VRF shape (top-level has ``routes``) or a
    nested ``{"<vrf>": {...}}`` map. Detect by presence of ``routes``.
    """
    if not isinstance(bgp_json, dict) or not bgp_json:
        return []
    if "routes" in bgp_json:
        return [(bgp_json.get("vrfName", "default"), bgp_json)]
    blocks: list[tuple[str, dict[str, Any]]] = []
    for k, v in bgp_json.items():
        if isinstance(v, dict) and "routes" in v:
            blocks.append((v.get("vrfName", k), v))
    return blocks


def _parse_as_path(path: Any) -> list[int] | None:
    """Convert FRR's space-separated AS_PATH string to a list of ints.

    Returns ``None`` for missing paths, ``[]`` for an empty iBGP path.
    Non-integer tokens (confederation segments, AS_SET) are skipped — those
    are rare enough in phase-2 topologies that we can defer full grammar
    support until a real diff exposes the gap.
    """
    if path is None:
        return None
    if not isinstance(path, str):
        return None
    tokens = path.strip().split()
    out: list[int] = []
    for t in tokens:
        try:
            out.append(int(t))
        except ValueError:
            continue
    return out
