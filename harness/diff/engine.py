"""FIB diff engine — Phase 4 deliverable.

Input: a per-topology workspace with up to three FIB sources keyed by
``(node, vrf, source)``:

- ``vendor`` — ground truth (FRR vtysh / cEOS show-command output)
- ``batfish`` — Batfish simulation output, canonicalized to the same schema
- ``hammerhead`` — Hammerhead simulation output, canonicalized ditto

Output: a list of :class:`DiffRecord` rows, one per ``(node, vrf, prefix)``
across the union of keys from the three sources, each carrying:

- ``presence`` — which sources carry the route
- ``next_hop_match`` — set equality across sources that have the route
- ``protocol_match`` — same protocol on both
- ``bgp_attrs_match`` — AS_PATH + LOCAL_PREF + MED equal, only when both
  sides report ``protocol == "bgp"``

The "two sides" of a comparison are always the ``compare`` source (batfish
or hammerhead) vs ``vendor``. A record keeps a separate match-bit for each
simulator so one row can answer "does batfish match vendor here?" and
"does hammerhead match vendor here?" at the same time.

Design principles:

- Pure functions; no file I/O. ``load_fib_workspace`` / ``diff_fibs`` take
  already-parsed NodeFibs as input. Tests build NodeFibs in-memory.
- Canonicalization happens up front (``canonicalize_node_fib`` on each
  input). After that, the diff is pure set/field comparison.
- The ``presence`` enum is authoritative — downstream metrics code should
  filter on it, not re-derive it from source-set membership.
- BGP attribute comparison is off by default for non-BGP routes. Routes
  whose protocol differs between the two sides have ``bgp_attrs_match ==
  None`` (not applicable) so downstream stats don't conflate a
  protocol-mismatch with a BGP-attribute-mismatch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from harness.aggregate import LoopbackPolicy
from harness.extract.fib import NextHop, NodeFib, Route, canonicalize_node_fib

__all__ = [
    "DiffRecord",
    "DiffWorkspace",
    "Presence",
    "diff_fibs",
    "load_fib_workspace",
]

Simulator = Literal["batfish", "hammerhead"]
Presence = Literal[
    "all-three",
    "vendor-and-batfish",
    "vendor-and-hammerhead",
    "batfish-and-hammerhead",
    "vendor-only",
    "batfish-only",
    "hammerhead-only",
]


@dataclass(frozen=True, slots=True)
class _RouteKey:
    """``(node, vrf, prefix)`` — one cell in the diff matrix."""

    node: str
    vrf: str
    prefix: str


@dataclass(slots=True)
class DiffRecord:
    """One row in the diff output. Serializable to JSON for reports."""

    node: str
    vrf: str
    prefix: str
    presence: Presence
    vendor_protocol: str | None = None
    batfish_protocol: str | None = None
    hammerhead_protocol: str | None = None
    vendor_next_hops: list[tuple[str | None, str | None]] = field(default_factory=list)
    batfish_next_hops: list[tuple[str | None, str | None]] = field(default_factory=list)
    hammerhead_next_hops: list[tuple[str | None, str | None]] = field(default_factory=list)
    # Per-simulator comparison bits. None means "not applicable" (one side is
    # missing this route) so aggregate metrics can separate coverage from
    # correctness.
    batfish_next_hop_match: bool | None = None
    hammerhead_next_hop_match: bool | None = None
    batfish_protocol_match: bool | None = None
    hammerhead_protocol_match: bool | None = None
    batfish_bgp_attrs_match: bool | None = None
    hammerhead_bgp_attrs_match: bool | None = None

    def as_dict(self) -> dict:
        """Return a JSON-serializable dict; tuple next-hops become [ip, iface] pairs."""
        d = asdict(self)
        for k in ("vendor_next_hops", "batfish_next_hops", "hammerhead_next_hops"):
            d[k] = [list(t) for t in d[k]]
        return d


@dataclass(slots=True)
class DiffWorkspace:
    """The three FIB sources for one topology, pre-canonicalized.

    Each list is ``NodeFib`` rows. Any of the three can be empty (e.g. Batfish
    failed to run, Hammerhead skipped). The diff engine handles missing
    sources gracefully — the ``presence`` field records what's actually
    there.
    """

    vendor: list[NodeFib] = field(default_factory=list)
    batfish: list[NodeFib] = field(default_factory=list)
    hammerhead: list[NodeFib] = field(default_factory=list)


# ---- main diff -----------------------------------------------------------


def diff_fibs(
    workspace: DiffWorkspace,
    *,
    filter_loopback_host: bool = False,
    loopback_policy: LoopbackPolicy | None = None,
) -> list[DiffRecord]:
    """Compare vendor / batfish / hammerhead FIBs cell-by-cell.

    Returns a list of :class:`DiffRecord` sorted by (node, vrf, prefix).
    Each source is canonicalized up front via :func:`canonicalize_node_fib`
    so callers can pass raw adapter output.

    Loopback handling is symmetric across all three sources, driven by the
    unified :class:`LoopbackPolicy` enum. Callers should prefer passing
    ``loopback_policy`` directly; ``filter_loopback_host`` is the legacy
    boolean bridge (maps to ``STRIP`` when True, ``PASSTHROUGH`` when False).
    If both arguments are supplied the enum wins.
    """
    policy = loopback_policy or LoopbackPolicy.from_bool(filter_loopback_host)
    vendor = _index_routes(workspace.vendor, loopback_policy=policy)
    batfish = _index_routes(workspace.batfish, loopback_policy=policy)
    hammerhead = _index_routes(workspace.hammerhead, loopback_policy=policy)

    keys: set[_RouteKey] = set(vendor) | set(batfish) | set(hammerhead)
    records: list[DiffRecord] = []
    for key in sorted(keys, key=lambda k: (k.node, k.vrf, k.prefix)):
        v = vendor.get(key)
        b = batfish.get(key)
        h = hammerhead.get(key)
        records.append(_build_record(key, v, b, h))
    return records


def _build_record(
    key: _RouteKey,
    v: Route | None,
    b: Route | None,
    h: Route | None,
) -> DiffRecord:
    presence = _presence(v, b, h)
    rec = DiffRecord(
        node=key.node,
        vrf=key.vrf,
        prefix=key.prefix,
        presence=presence,
        vendor_protocol=v.protocol if v else None,
        batfish_protocol=b.protocol if b else None,
        hammerhead_protocol=h.protocol if h else None,
        vendor_next_hops=_nh_pairs(v.next_hops) if v else [],
        batfish_next_hops=_nh_pairs(b.next_hops) if b else [],
        hammerhead_next_hops=_nh_pairs(h.next_hops) if h else [],
    )
    if v is not None and b is not None:
        rec.batfish_next_hop_match = _nh_sets_equal(v.next_hops, b.next_hops)
        rec.batfish_protocol_match = v.protocol == b.protocol
        rec.batfish_bgp_attrs_match = _bgp_attrs_match(v, b)
    if v is not None and h is not None:
        rec.hammerhead_next_hop_match = _nh_sets_equal(v.next_hops, h.next_hops)
        rec.hammerhead_protocol_match = v.protocol == h.protocol
        rec.hammerhead_bgp_attrs_match = _bgp_attrs_match(v, h)
    return rec


_PRESENCE_TABLE: dict[tuple[bool, bool, bool], Presence] = {
    (True, True, True): "all-three",
    (True, True, False): "vendor-and-batfish",
    (True, False, True): "vendor-and-hammerhead",
    (False, True, True): "batfish-and-hammerhead",
    (True, False, False): "vendor-only",
    (False, True, False): "batfish-only",
    (False, False, True): "hammerhead-only",
}


def _presence(v: Route | None, b: Route | None, h: Route | None) -> Presence:
    flags = (v is not None, b is not None, h is not None)
    presence = _PRESENCE_TABLE.get(flags)
    if presence is None:  # pragma: no cover — set-union means at least one is True
        raise RuntimeError("unreachable: presence with no sources")
    return presence


def _nh_pairs(nhs: Iterable[NextHop]) -> list[tuple[str | None, str | None]]:
    return [(n.ip, n.interface) for n in nhs]


def _nh_sets_equal(a: Iterable[NextHop], b: Iterable[NextHop]) -> bool:
    """Set-equality over (ip, iface). next-hops were canonicalized so order matches,
    but using frozenset here is strictly safer against future canonicalization
    changes."""
    return frozenset(_nh_pairs(a)) == frozenset(_nh_pairs(b))


def _bgp_attrs_match(a: Route, b: Route) -> bool | None:
    """Compare AS_PATH + LOCAL_PREF + MED. Returns None if either side isn't BGP."""
    if a.protocol != "bgp" or b.protocol != "bgp":
        return None
    return (
        _as_path_equal(a.as_path, b.as_path)
        and a.local_pref == b.local_pref
        and a.med == b.med
    )


def _as_path_equal(a: list[int] | None, b: list[int] | None) -> bool:
    """None matches None, [] matches []. Length + order sensitive otherwise."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def _index_routes(
    fibs: Iterable[NodeFib],
    *,
    loopback_policy: LoopbackPolicy,
) -> dict[_RouteKey, Route]:
    """Build a ``{(node, vrf, prefix): Route}`` index.

    If the same key appears twice (two FIB files for the same (node, vrf)),
    the later one wins. Callers are responsible for not feeding duplicates;
    the extract layer already writes one JSON per (node, vrf).
    """
    out: dict[_RouteKey, Route] = {}
    for raw in fibs:
        fib = canonicalize_node_fib(raw, loopback_policy=loopback_policy)
        for r in fib.routes:
            out[_RouteKey(node=fib.node, vrf=fib.vrf, prefix=r.prefix)] = r
    return out


# ---- workspace loader ----------------------------------------------------


def load_fib_workspace(
    results_dir: Path,
    topology: str,
) -> DiffWorkspace:
    """Load vendor / batfish / hammerhead FIB JSON files from disk.

    Layout (convention, matches ``pipeline.run_topology``):

    ``results_dir/vendor_truth/<topology>/<node>__<vrf>.json``
    ``results_dir/batfish/<topology>/<node>__<vrf>.json``
    ``results_dir/hammerhead/<topology>/<node>__<vrf>.json``

    Missing source directories silently produce an empty list for that
    source — the diff engine's ``presence`` field captures that signal.
    """
    return DiffWorkspace(
        vendor=_load_dir(results_dir / "vendor_truth" / topology),
        batfish=_load_dir(results_dir / "batfish" / topology),
        hammerhead=_load_dir(results_dir / "hammerhead" / topology),
    )


def _load_dir(dirpath: Path) -> list[NodeFib]:
    if not dirpath.exists():
        return []
    fibs: list[NodeFib] = []
    for p in sorted(dirpath.glob("*.json")):
        # Skip per-tool stats sidecars (e.g. ``batfish_stats.json``,
        # ``hammerhead_stats.json``) that live in the same directory.
        if p.name.endswith("_stats.json"):
            continue
        fibs.append(NodeFib.model_validate_json(p.read_text()))
    return fibs
