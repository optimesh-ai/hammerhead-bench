"""Arista cEOS-lab adapter — vendor ground truth via ``docker exec ... Cli``.

The adapter mirrors the :class:`~harness.adapters.frr.FrrAdapter` shape so the
pipeline sees a single abstract ``VendorAdapter`` per node. The only knobs
that differ from FRR are:

- The clab kind is ``ceos`` (not ``linux``), and the config is shipped via
  the ``startup-config`` clab key rather than a bind-mount.
- Memory cap defaults to 2048 MB — cEOS refuses to boot below ~1.5 GB and
  the upstream Arista sizing guidance is "2 GB+ for labs".
- The runtime shell is cEOS's ``Cli`` binary. ``docker exec <container> Cli
  -c '<command> | json'`` returns JSON on stdout the same way vtysh does.

Convergence + extraction contract (per spec):

- All configured BGP sessions in ``Established`` (EOS calls this
  ``peerState == Established``).
- Route count stable across two consecutive 15 s samples.
- FIB read via ``show ip route vrf all | json``; BGP attributes merged
  from ``show ip bgp vrf all | json``.
- Hard cap 5 min; missing convergence marks the topology failed.

cEOS image: user-supplied via ``CEOS_IMAGE`` env var or adapter constructor
(Arista EOS Central, free account; licensing forbids us bundling it). The
pilot default ``ceos:4.32.0F`` is what the Phase 8 spec was authored
against; any image >= 4.29 should work for the pure ACL / OSPF corpus we
ship today.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.adapters.base import VendorAdapter
from harness.extract.fib import (
    NodeFib,
    merge_bgp_attributes,
    parse_eos_route_json,
)

CEOS_DEFAULT_MEMORY_MB = 2048
CEOS_DEFAULT_IMAGE = "ceos:4.32.0F"
CONVERGENCE_SAMPLE_INTERVAL_S = 15
CONVERGENCE_TIMEOUT_S = 300
CLI_TIMEOUT_S = 30


class CeosCliError(RuntimeError):
    """Raised when ``docker exec ... Cli`` fails. Never swallowed silently."""


def _resolve_default_image() -> str:
    """Honour ``$CEOS_IMAGE`` so operators with a specific licensed tag can
    override without editing source. Falls back to :data:`CEOS_DEFAULT_IMAGE`.
    """
    return os.environ.get("CEOS_IMAGE", CEOS_DEFAULT_IMAGE).strip() or CEOS_DEFAULT_IMAGE


@dataclass(frozen=True, slots=True)
class CeosAdapter(VendorAdapter):
    """Per-container cEOS wrapper. One instance per distinct memory/image combo."""

    image: str = ""
    memory_mb: int = CEOS_DEFAULT_MEMORY_MB
    kind: str = "ceos"
    config_template_names: tuple[str, ...] = ("startup-config.j2",)

    def __post_init__(self) -> None:
        if not self.image:
            object.__setattr__(self, "image", _resolve_default_image())

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        """Return the dict that goes under ``topology.nodes[<name>]`` in clab YAML.

        ``config_path`` is the per-node directory containing ``startup-config``.
        clab copies that file into ``/mnt/flash/startup-config`` inside the
        cEOS container on first boot.
        """
        _ = name  # part of the Protocol contract; clab reads the name from YAML
        return {
            "kind": "ceos",
            "image": self.image,
            "memory": f"{self.memory_mb}m",
            "startup-config": f"{config_path}/startup-config",
        }

    # ----- runtime queries (docker exec) -----

    def _cli(self, container: str, cmd: str) -> str:
        """Run one Cli command; return stdout. Raises on non-zero exit."""
        proc = subprocess.run(
            ["docker", "exec", container, "Cli", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise CeosCliError(f"{container}: Cli {cmd!r} failed: {err}")
        return proc.stdout

    def wait_for_convergence(self, container: str, timeout_s: int = CONVERGENCE_TIMEOUT_S) -> bool:
        """Block until all BGP sessions are Established AND route count is stable.

        Returns ``True`` on convergence, ``False`` on timeout. Never raises on
        transient Cli failures — those count as "not yet converged" and we
        keep polling until the timeout. Matches :class:`FrrAdapter`'s contract
        so the pipeline code path is symmetric.
        """
        deadline = time.monotonic() + timeout_s
        prev_count: int | None = None
        stable_since: float | None = None

        while time.monotonic() < deadline:
            try:
                summary = self._cli(container, "show ip bgp summary | json")
                bgp_ok = _all_bgp_sessions_established(summary)
                count = _total_route_count(self._cli(container, "show ip route vrf all | json"))
            except (CeosCliError, subprocess.TimeoutExpired, json.JSONDecodeError):
                time.sleep(1.0)
                continue

            if not bgp_ok:
                prev_count = None
                stable_since = None
                time.sleep(2.0)
                continue

            if prev_count == count:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= CONVERGENCE_SAMPLE_INTERVAL_S:
                    return True
            else:
                stable_since = None

            prev_count = count
            time.sleep(CONVERGENCE_SAMPLE_INTERVAL_S)

        return False

    def extract_fib(self, container: str, node_name: str | None = None) -> list[NodeFib]:
        """Pull the full FIB and return one ``NodeFib`` per (node, vrf).

        ``node_name`` overrides the container name in the emitted NodeFib
        objects (e.g. ``clab-topo-r2`` -> ``r2``). If ``None``, ``container``
        is used as-is.
        """
        name = node_name or container
        route_json = json.loads(self._cli(container, "show ip route vrf all | json"))
        bgp_raw = self._cli(container, "show ip bgp vrf all | json")
        bgp_json = json.loads(bgp_raw) if bgp_raw.strip() else {}

        fibs = parse_eos_route_json(route_json, node_name=name, source="vendor")
        bgp_by_vrf = _flatten_eos_bgp(bgp_json)
        return [merge_bgp_attributes(f, bgp_by_vrf.get(f.vrf, {})) for f in fibs]


# ----- helpers (module-private) --------------------------------------------


def _all_bgp_sessions_established(bgp_summary_json: str) -> bool:
    """Return True iff every neighbor in every VRF reports Established.

    EOS shape::

        {"vrfs": {"<vrf>": {"peers": {"<neighbor>": {"peerState": "Established"}}}}}

    Empty output (no BGP configured) also returns True — a topology with no
    BGP sessions is trivially converged for this check.
    """
    data = json.loads(bgp_summary_json) if bgp_summary_json.strip() else {}
    if not isinstance(data, dict):
        return False
    vrfs = data.get("vrfs")
    if not isinstance(vrfs, dict):
        return True  # no BGP configured → trivially converged
    for vrf_body in vrfs.values():
        if not isinstance(vrf_body, dict):
            continue
        peers = vrf_body.get("peers", {})
        if not isinstance(peers, dict):
            continue
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            state = peer.get("peerState", "") or peer.get("bgpState", "")
            if state != "Established":
                return False
    return True


def _total_route_count(route_vrf_all_json: str) -> int:
    """Return the total number of (prefix, vrf) entries across every VRF.

    EOS shape (multi-VRF native)::

        {"vrfs": {"<vrf>": {"routes": {"<prefix>": {...}, ...}}, ...}}
    """
    data = json.loads(route_vrf_all_json) if route_vrf_all_json.strip() else {}
    if not isinstance(data, dict):
        return 0
    total = 0
    for vrf_body in data.get("vrfs", {}).values():
        if not isinstance(vrf_body, dict):
            continue
        routes = vrf_body.get("routes", {})
        if isinstance(routes, dict):
            total += len(routes)
    return total


def _flatten_eos_bgp(bgp_json: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten EOS's ``show ip bgp vrf all | json`` into a per-VRF block in
    the FRR shape that :func:`merge_bgp_attributes` already understands.

    EOS::

        {"vrfs": {"<vrf>": {"bgpRouteEntries": {"<prefix>": {"bgpRoutePaths":
          [{"asPathEntry": {"asPath": "65001 65002"}, "localPreference": 100,
            "med": 0, "reasonNotBestpath": null}, ...]}, ...}}}}

    Canonical (FRR-ish)::

        {"<vrf>": {"routes": {"<prefix>": [{"bestpath": True, "path": "...",
          "locPrf": 100, "metric": 0}]}}}
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(bgp_json, dict):
        return out
    for vrf_name, body in bgp_json.get("vrfs", {}).items():
        if not isinstance(body, dict):
            continue
        routes: dict[str, list[dict[str, Any]]] = {}
        for prefix, entry in body.get("bgpRouteEntries", {}).items():
            if not isinstance(entry, dict):
                continue
            paths: list[dict[str, Any]] = []
            for p in entry.get("bgpRoutePaths", []):
                if not isinstance(p, dict):
                    continue
                as_path = ""
                as_entry = p.get("asPathEntry")
                if isinstance(as_entry, dict):
                    as_path = str(as_entry.get("asPath") or "")
                paths.append(
                    {
                        "bestpath": p.get("reasonNotBestpath") in (None, ""),
                        "path": as_path,
                        "locPrf": p.get("localPreference"),
                        "metric": p.get("med"),
                    }
                )
            if paths:
                routes[prefix] = paths
        out[vrf_name] = {"routes": routes, "vrfName": vrf_name}
    return out
