"""pybatfish wrapper — Phase 5 deliverable.

Two layers:

1. :func:`transform_batfish_rows` — a pure function that takes
   ``routes()`` + ``bgpRib()`` row dicts (as produced by
   ``pybatfish.client.session.Session.q.routes().answer().frame().to_dict(
   orient="records")``) and returns canonical :class:`NodeFib` rows,
   one per (node, vrf). Zero I/O. Fully test-covered.

2. :func:`run_batfish` — orchestration. Starts a Batfish container via the
   ``BatfishRunner`` protocol (default = ``DockerBatfishRunner``, which
   shells out to ``docker run``), waits until the REST API answers, uploads
   the snapshot via the injected :class:`BatfishSession` factory, pulls
   ``routes()`` + ``bgpRib()`` frames, converts to NodeFibs, writes per-(node,
   vrf) JSON files, and tears the container down. The two protocols are
   test seams so the orchestration path is exercisable without a real
   Batfish install.

Batfish runs with ``-e _JAVA_OPTIONS=-Xmx4g`` to cap the JVM; the
container is pinned by digest in ``versions.lock`` — the pipeline reads
``BATFISH_IMAGE`` from there. A per-topology ``batfish_stats.json`` lands
alongside the FIB JSON so reports know init + query wall-time.

Memory discipline:

- The harness already holds the host memory guard. This wrapper adds nothing
  new; it only surfaces the per-container 4 GiB cap in the manifest so the
  pipeline's ``sum_container_limits_mb`` math is correct when Batfish is on.
- The container is destroyed in a ``finally`` so a failed snapshot init
  doesn't leak a running JVM.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.extract.fib import (
    NextHop,
    NodeFib,
    Route,
    canonicalize_node_fib,
    canonicalize_vrf,
)
from harness.extract.fib import (
    Protocol as _FibProtocol,
)

log = logging.getLogger(__name__)

__all__ = [
    "BATFISH_MEMORY_MB",
    "BatfishConfig",
    "BatfishRunner",
    "BatfishSession",
    "BatfishStats",
    "DockerBatfishRunner",
    "run_batfish",
    "transform_batfish_rows",
]

# Per-container memory cap applied by `-e _JAVA_OPTIONS=-Xmx4g`. Surfaced as a
# constant so the pipeline's host-headroom math can sum it in.
BATFISH_MEMORY_MB = 4096


# ---- schema transform ---------------------------------------------------

# Batfish surfaces protocol labels like "ibgp" / "ebgp" / "connected". Map them
# to our canonical Protocol literal. Unknown labels raise ValueError so schema
# drift surfaces loudly (same policy as the FRR parser).
_BATFISH_PROTOCOL_MAP: dict[str, _FibProtocol] = {
    "connected": "connected",
    "local": "local",
    "static": "static",
    "ospf": "ospf",
    "ospf-inter": "ospf",
    "ospf-intra": "ospf",
    "ospf-external-type-1": "ospf",
    "ospf-external-type-2": "ospf",
    "ospf-ia": "ospf",
    "ospf-e1": "ospf",
    "ospf-e2": "ospf",
    "bgp": "bgp",
    "ibgp": "bgp",
    "ebgp": "bgp",
    "aggregate": "bgp",
    "isis-l1": "isis",
    "isis-l2": "isis",
    "isis": "isis",
    "rip": "rip",
}


def _map_batfish_protocol(raw: str) -> _FibProtocol | None:
    """Return canonical protocol or None if Batfish labels it as something we skip.

    Skipped categories: ``kernel``, ``aggregate`` for non-BGP topologies,
    Batfish's ``ospfE1IntraArea`` permutations are already flattened above.
    Raises ``ValueError`` for genuinely unknown strings so the diff layer
    never silently drops routes.
    """
    norm = (raw or "").strip().lower().replace(" ", "-")
    if norm in {"kernel", "unknown"}:
        return None
    mapped = _BATFISH_PROTOCOL_MAP.get(norm)
    if mapped is None:
        raise ValueError(
            f"unknown Batfish protocol {raw!r}; update _BATFISH_PROTOCOL_MAP "
            "in harness/tools/batfish.py"
        )
    return mapped


def transform_batfish_rows(
    route_rows: list[dict[str, Any]],
    *,
    bgp_rows: list[dict[str, Any]] | None = None,
) -> list[NodeFib]:
    """Convert Batfish row dicts to canonical :class:`NodeFib` records.

    ``route_rows`` is the ``routes()`` answer as a list of dicts. Each row
    carries at minimum:

    - ``Node`` — hostname string
    - ``VRF`` — vrf name
    - ``Network`` — CIDR prefix
    - ``Protocol`` — protocol label (see ``_BATFISH_PROTOCOL_MAP``)
    - ``Next_Hop`` *or* ``Next_Hop_IP``/``Next_Hop_Interface`` — depends on
      the Batfish version. We read both shapes.
    - ``Admin_Distance``, ``Metric`` — optional

    ``bgp_rows`` is the ``bgpRib()`` answer (also a list of dicts). When
    provided, AS_PATH / LOCAL_PREF / MED attributes are attached to the BGP
    routes by (Node, VRF, Network) join.

    Returns one ``NodeFib`` per (node, vrf), pre-canonicalized (next-hops
    sorted, VRF alias collapsed).
    """
    # (node, vrf) -> list[Route]
    fibs: dict[tuple[str, str], list[Route]] = {}
    for row in route_rows:
        node, vrf, route = _row_to_route(row)
        if route is None:
            continue
        fibs.setdefault((node, vrf), []).append(route)

    if bgp_rows:
        _merge_bgp_attrs(fibs, bgp_rows)

    result: list[NodeFib] = []
    for (node, vrf), routes in sorted(fibs.items()):
        nf = NodeFib(node=node, vrf=vrf, source="batfish", routes=routes)
        result.append(canonicalize_node_fib(nf))
    return result


def _row_to_route(row: dict[str, Any]) -> tuple[str, str, Route | None]:
    node = str(row.get("Node") or "").strip()
    vrf = canonicalize_vrf(str(row.get("VRF") or "default"))
    prefix = str(row.get("Network") or "").strip()
    if not node or not prefix:
        return node, vrf, None
    proto = _map_batfish_protocol(str(row.get("Protocol") or ""))
    if proto is None:
        return node, vrf, None
    nhs = _row_to_next_hops(row)
    route = Route(
        prefix=prefix,
        protocol=proto,
        next_hops=nhs,
        admin_distance=_as_int(row.get("Admin_Distance")),
        metric=_as_int(row.get("Metric")),
    )
    return node, vrf, route


def _row_to_next_hops(row: dict[str, Any]) -> list[NextHop]:
    """Accept either ``Next_Hop``: dict form or flat ``Next_Hop_IP``/``Next_Hop_Interface``.

    Batfish 2023+ ships the dict shape; older pybatfish versions produce the
    flat columns. Both are tolerated so the transform is stable across
    upgrades.
    """
    nh_dict = row.get("Next_Hop")
    if isinstance(nh_dict, dict):
        ip = _nh_ip(nh_dict)
        iface = _nh_iface(nh_dict)
        if ip is None and iface is None:
            return []
        return [NextHop(ip=ip, interface=iface)]
    # Flat form. Batfish sometimes emits a single pair, sometimes a list.
    ip_val = row.get("Next_Hop_IP")
    iface_val = row.get("Next_Hop_Interface")
    if isinstance(ip_val, list):
        ips = ip_val
        ifaces = iface_val if isinstance(iface_val, list) else [iface_val] * len(ips)
        return [
            NextHop(ip=_none_or_str(ip), interface=_none_or_str(ifc))
            for ip, ifc in zip(ips, ifaces, strict=False)
            if (ip is not None) or (ifc is not None)
        ]
    ip = _none_or_str(ip_val)
    iface = _none_or_str(iface_val)
    if ip is None and iface is None:
        return []
    return [NextHop(ip=ip, interface=iface)]


def _nh_ip(d: dict[str, Any]) -> str | None:
    ip = d.get("ip") or d.get("nextHopIp") or d.get("Next_Hop_IP")
    return _none_or_str(ip)


def _nh_iface(d: dict[str, Any]) -> str | None:
    iface = d.get("interface") or d.get("nextHopInterface") or d.get("Next_Hop_Interface")
    # Batfish sometimes emits "dynamic" as the interface when next-hop is
    # recursive and unresolved; treat that as no interface.
    if iface in ("dynamic", "null_interface"):
        return None
    return _none_or_str(iface)


def _merge_bgp_attrs(
    fibs: dict[tuple[str, str], list[Route]],
    bgp_rows: list[dict[str, Any]],
) -> None:
    """Attach AS_PATH / LOCAL_PREF / MED to BGP-best routes by (node, vrf, prefix)."""
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in bgp_rows:
        # bgpRib rows with Status containing "BEST" are in the RIB.
        statuses = row.get("Status")
        if isinstance(statuses, list):
            if not any(str(s).upper().startswith("BEST") for s in statuses):
                continue
        elif isinstance(statuses, str) and "BEST" not in statuses.upper():
            continue
        node = str(row.get("Node") or "").strip()
        vrf = canonicalize_vrf(str(row.get("VRF") or "default"))
        prefix = str(row.get("Network") or "").strip()
        if not node or not prefix:
            continue
        best[(node, vrf, prefix)] = row

    for (node, vrf), routes in fibs.items():
        for i, r in enumerate(routes):
            if r.protocol != "bgp":
                continue
            br = best.get((node, vrf, r.prefix))
            if br is None:
                continue
            # `X or Y` falls through on 0 — BGP MED/LOCAL_PREF can be 0, so pick
            # the first *present* key rather than the first truthy one.
            as_path_raw = br.get("AS_Path") if "AS_Path" in br else br.get("As_Path")
            lp_raw = br.get("Local_Pref") if "Local_Pref" in br else br.get("LocalPref")
            med_raw = br.get("Metric") if "Metric" in br else br.get("Med")
            routes[i] = r.model_copy(
                update={
                    "as_path": _parse_as_path(as_path_raw),
                    "local_pref": _as_int(lp_raw),
                    "med": _as_int(med_raw),
                    "communities": _parse_communities(br.get("Communities")),
                }
            )


def _parse_as_path(val: Any) -> list[int] | None:
    if val is None:
        return None
    if isinstance(val, list):
        out: list[int] = []
        for x in val:
            try:
                out.append(int(x))
            except (ValueError, TypeError):
                continue
        return out
    if isinstance(val, str):
        out2: list[int] = []
        for tok in val.strip().split():
            try:
                out2.append(int(tok))
            except ValueError:
                continue
        return out2
    return None


def _parse_communities(val: Any) -> list[str] | None:
    if val is None:
        return None
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str):
        return [s.strip() for s in val.split() if s.strip()]
    return None


def _as_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _none_or_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    return s


# ---- orchestration -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatfishConfig:
    """Everything the runner needs to start Batfish. All fields have sane defaults."""

    image: str = "batfish/allinone:latest"
    coordinator_port: int = 9997
    service_port: int = 9996
    memory_mb: int = BATFISH_MEMORY_MB
    startup_timeout_s: int = 180
    container_name_prefix: str = "hh-bench-batfish"


@dataclass(slots=True)
class BatfishStats:
    """Written alongside the FIB JSON so reports can surface per-topology timing."""

    topology: str
    started_iso: str
    init_snapshot_s: float
    query_routes_s: float
    query_bgp_s: float
    total_s: float

    def as_dict(self) -> dict:
        return asdict(self)


class BatfishSession(Protocol):
    """Abstract pybatfish session so tests can inject a fake."""

    def init_snapshot(self, path: str, name: str, overwrite: bool = True) -> str: ...

    def get_routes(self) -> list[dict[str, Any]]: ...

    def get_bgp_rib(self) -> list[dict[str, Any]]: ...


class BatfishRunner(Protocol):
    """Abstract Batfish container lifecycle so tests can skip docker."""

    def start(self, cfg: BatfishConfig) -> str: ...

    def wait_ready(self, cfg: BatfishConfig) -> None: ...

    def stop(self, container_id: str) -> None: ...


@dataclass(slots=True)
class DockerBatfishRunner:
    """Production runner: shells out to ``docker``. Not used in tests."""

    run_cmd: Callable[[list[str]], tuple[int, str, str]] = field(
        default_factory=lambda: _docker_run
    )

    def start(self, cfg: BatfishConfig) -> str:
        name = f"{cfg.container_name_prefix}-{int(time.time())}"
        rc, out, err = self.run_cmd(
            [
                "docker", "run", "-d", "--rm",
                "--name", name,
                "-e", f"_JAVA_OPTIONS=-Xmx{cfg.memory_mb}m",
                "-p", f"{cfg.coordinator_port}:{cfg.coordinator_port}",
                "-p", f"{cfg.service_port}:{cfg.service_port}",
                cfg.image,
            ]
        )
        if rc != 0:
            raise RuntimeError(f"docker run batfish failed: {err or out}")
        return out.strip() or name

    def wait_ready(self, cfg: BatfishConfig) -> None:  # pragma: no cover - live docker only
        # Poll via `docker logs` for the readiness banner. We don't depend on
        # the REST API being reachable from the harness host to avoid wiring
        # requests in here.
        deadline = time.monotonic() + cfg.startup_timeout_s
        while time.monotonic() < deadline:
            rc, out, _ = self.run_cmd(["docker", "logs", cfg.container_name_prefix])
            if rc == 0 and ("Service is up" in out or "Serving on" in out):
                return
            time.sleep(2.0)
        raise TimeoutError(f"Batfish did not start within {cfg.startup_timeout_s}s")

    def stop(self, container_id: str) -> None:
        rc, _, err = self.run_cmd(["docker", "rm", "-f", container_id])
        if rc != 0:
            raise RuntimeError(f"docker rm batfish failed: {err}")


def _docker_run(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover - thin wrapper
    import subprocess  # noqa: PLC0415 — local import; not needed in tests
    p = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300)
    return p.returncode, p.stdout, p.stderr


def run_batfish(
    configs_dir: Path,
    out_dir: Path,
    *,
    topology: str,
    session_factory: Callable[[BatfishConfig], BatfishSession] | None = None,
    runner: BatfishRunner | None = None,
    config: BatfishConfig | None = None,
) -> BatfishStats:
    """Run Batfish over ``configs_dir``, write per-(node, vrf) FIBs to ``out_dir``.

    ``session_factory`` and ``runner`` are test seams. Production code leaves
    both ``None`` and a default pybatfish-backed session is used.

    Returns a :class:`BatfishStats` with per-phase timing. Raises
    ``RuntimeError`` / ``TimeoutError`` on any underlying failure; the
    container is always stopped on exit.
    """
    cfg = config or BatfishConfig()
    runner = runner or DockerBatfishRunner()
    if session_factory is None:
        session_factory = _default_pybatfish_session_factory

    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
    t0 = time.monotonic()

    container_id = runner.start(cfg)
    try:
        runner.wait_ready(cfg)
        session = session_factory(cfg)

        t_init = time.monotonic()
        session.init_snapshot(str(configs_dir), name=f"bench-{topology}", overwrite=True)
        init_s = time.monotonic() - t_init

        t_routes = time.monotonic()
        route_rows = session.get_routes()
        routes_s = time.monotonic() - t_routes

        t_bgp = time.monotonic()
        bgp_rows = session.get_bgp_rib()
        bgp_s = time.monotonic() - t_bgp

        fibs = transform_batfish_rows(route_rows, bgp_rows=bgp_rows)
        out_dir.mkdir(parents=True, exist_ok=True)
        for fib in fibs:
            out_path = out_dir / f"{fib.node}__{fib.vrf}.json"
            out_path.write_text(fib.model_dump_json(indent=2) + "\n")
    finally:
        try:
            runner.stop(container_id)
        except Exception as exc:  # noqa: BLE001 — stop failure is non-fatal; logged
            log.warning("batfish: container stop failed: %s", exc)

    stats = BatfishStats(
        topology=topology,
        started_iso=started_iso,
        init_snapshot_s=init_s,
        query_routes_s=routes_s,
        query_bgp_s=bgp_s,
        total_s=time.monotonic() - t0,
    )
    (out_dir / "batfish_stats.json").write_text(json.dumps(stats.as_dict(), indent=2) + "\n")
    return stats


def _default_pybatfish_session_factory(  # pragma: no cover - live-only
    cfg: BatfishConfig,
) -> BatfishSession:
    """Lazy-import pybatfish so tests that don't touch Batfish don't pay the import cost.

    Production path only. A thin adapter wraps pybatfish's Session so the
    protocol methods match our abstract surface.
    """
    from pybatfish.client.session import Session  # noqa: PLC0415

    class _PybatfishAdapter:
        def __init__(self, inner: Session) -> None:
            self._s = inner

        def init_snapshot(self, path: str, name: str, overwrite: bool = True) -> str:
            return self._s.init_snapshot(path, name=name, overwrite=overwrite)

        def get_routes(self) -> list[dict[str, Any]]:
            return self._s.q.routes().answer().frame().to_dict(orient="records")

        def get_bgp_rib(self) -> list[dict[str, Any]]:
            return self._s.q.bgpRib().answer().frame().to_dict(orient="records")

    inner = Session(host="localhost")
    return _PybatfishAdapter(inner)
