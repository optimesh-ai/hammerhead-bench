"""FRRouting adapter — vendor ground truth via ``docker exec ... vtysh``.

The adapter is a pure wrapper around the running container: it owns no state,
the pipeline owns the container name and memory cap. Every method is idempotent
and safe to retry.

Convergence detection (per spec):

- All configured BGP sessions in ``Established``.
- Route count stable across two consecutive 15 s samples.
- Hard cap 5 min; missing convergence marks the topology failed.

FIB extraction:

- ``vtysh -c 'show ip route vrf all json'`` → per-VRF prefix map.
- ``vtysh -c 'show ip bgp vrf all json'`` → BGP attributes (AS_PATH, LOCAL_PREF,
  MED) merged in for protocol=="bgp" rows.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.adapters.base import VendorAdapter
from harness.extract.fib import NodeFib, merge_bgp_attributes, parse_frr_route_json

FRR_DEFAULT_MEMORY_MB = 256
CONVERGENCE_SAMPLE_INTERVAL_S = 15
CONVERGENCE_TIMEOUT_S = 300
VTYSH_TIMEOUT_S = 30


class FrrVtyshError(RuntimeError):
    """Raised when ``docker exec ... vtysh`` fails. Never swallowed silently."""


@dataclass(frozen=True, slots=True)
class FrrAdapter(VendorAdapter):
    """Per-container FRR wrapper. One instance per distinct memory/image combo.

    The same instance is shared across every node in a topology that wants the
    default config. Per-node overrides live in ``Node.params`` and are rendered
    into the config template.
    """

    image: str = "frrouting/frr:v8.4.1"
    memory_mb: int = FRR_DEFAULT_MEMORY_MB

    kind: str = "frr"
    config_template_names: tuple[str, ...] = ("frr.conf.j2", "daemons.j2")

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        """Return the dict that goes under ``topology.nodes[<name>]`` in clab YAML.

        ``config_path`` must be the per-node directory containing ``frr.conf``
        and ``daemons`` files. Bind-mounts them into ``/etc/frr`` inside the
        container so FRR boots from the rendered config.
        """
        return {
            "kind": "linux",
            "image": self.image,
            "memory": f"{self.memory_mb}m",
            "binds": [
                f"{config_path}/frr.conf:/etc/frr/frr.conf",
                f"{config_path}/daemons:/etc/frr/daemons",
            ],
        }

    # ----- runtime queries (docker exec) -----

    def _vtysh(self, container: str, cmd: str) -> str:
        """Run one vtysh command; return stdout. Raises on non-zero exit."""
        proc = subprocess.run(
            ["docker", "exec", container, "vtysh", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=VTYSH_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise FrrVtyshError(f"{container}: vtysh {cmd!r} failed: {err}")
        return proc.stdout

    def wait_for_convergence(self, container: str, timeout_s: int = CONVERGENCE_TIMEOUT_S) -> bool:
        """Block until all BGP sessions are Established AND route count is stable.

        Returns ``True`` on convergence, ``False`` on timeout. Never raises on
        transient vtysh failures — those count as "not yet converged" and we
        keep polling until the timeout.
        """
        deadline = time.monotonic() + timeout_s
        prev_count: int | None = None
        stable_since: float | None = None

        while time.monotonic() < deadline:
            try:
                summary = self._vtysh(container, "show bgp summary json")
                bgp_ok = _all_bgp_sessions_established(summary)
                count = _total_route_count(self._vtysh(container, "show ip route vrf all json"))
            except (FrrVtyshError, subprocess.TimeoutExpired, json.JSONDecodeError):
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
        objects (e.g. ``clab-topo-r1`` -> ``r1``). If ``None``, ``container``
        is used as-is.
        """
        name = node_name or container
        route_json = json.loads(self._vtysh(container, "show ip route vrf all json"))
        bgp_json_raw = self._vtysh(container, "show ip bgp vrf all json")
        bgp_json = json.loads(bgp_json_raw) if bgp_json_raw.strip() else {}

        fibs = parse_frr_route_json(route_json, node_name=name, source="vendor")
        return [merge_bgp_attributes(f, bgp_json) for f in fibs]


# ----- helpers (module-private, used by wait_for_convergence) -----


def _all_bgp_sessions_established(bgp_summary_json: str) -> bool:
    """Return True iff every neighbor in every VRF reports ``state == Established``.

    Empty output (no BGP configured) also returns True: a topology with no BGP
    sessions is trivially converged for this check.
    """
    data = json.loads(bgp_summary_json) if bgp_summary_json.strip() else {}
    if not isinstance(data, dict):
        return False

    # FRR returns either a single VRF dict or a top-level map of
    # vrfName -> VrfSummary. Normalize to a list of VRF summaries.
    vrfs: list[dict[str, Any]]
    if "ipv4Unicast" in data or "peers" in data:
        vrfs = [data]
    else:
        vrfs = [v for v in data.values() if isinstance(v, dict)]

    saw_any_peer = False
    for vrf in vrfs:
        af = vrf.get("ipv4Unicast", vrf)
        peers = af.get("peers", {}) if isinstance(af, dict) else {}
        for peer in peers.values():
            saw_any_peer = True
            state = peer.get("state", "")
            if state != "Established":
                return False
    # If BGP is configured but no peers are up, saw_any_peer was True above
    # and the early return fired. If BGP isn't configured, saw_any_peer is
    # False — trivially converged.
    _ = saw_any_peer
    return True


def _total_route_count(route_vrf_all_json: str) -> int:
    """Return the total number of (prefix, vrf) entries in the FIB."""
    data = json.loads(route_vrf_all_json) if route_vrf_all_json.strip() else {}
    if not isinstance(data, dict):
        return 0
    total = 0
    # Either flat prefix -> entries dict (single VRF) or vrfName -> prefix dict.
    if data and all(isinstance(v, list) for v in data.values()):
        total = len(data)
    else:
        for vrf_body in data.values():
            if isinstance(vrf_body, dict):
                total += len(vrf_body)
    return total
