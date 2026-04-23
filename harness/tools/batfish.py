"""pybatfish wrapper — Phase 5 deliverable.

Three layers:

1. :func:`transform_batfish_rows` — a pure function that takes
   ``routes()`` + ``bgpRib()`` row dicts (as produced by
   ``pybatfish.client.session.Session.q.routes().answer().frame().to_dict(
   orient="records")``) and returns canonical :class:`NodeFib` rows,
   one per (node, vrf). Zero I/O. Fully test-covered.

2. :class:`BatfishService` — a long-lived container + session. ``start()``
   pays the JVM cold-start *once*; ``run_one(configs_dir, out_dir, topology)``
   can then be called N times while the JVM stays warm. Per-call
   :class:`BatfishStats` carry ``warm`` = False on the first call and
   ``True`` on every subsequent call, so the harness can separate
   "infrastructure overhead" (container start + REST readiness) from
   "warm JVM solve latency" as requested by reviewers who want to see
   the per-trial timings on a persistent service rather than a
   per-trial cold container.

3. :func:`run_batfish` — legacy one-shot orchestration. Equivalent to
   ``with BatfishService(...) as svc: return svc.run_one(...)``. Kept
   verbatim for backward compatibility with callers that do not want
   to manage a persistent service (the 3-way truth path, one-off
   smoke tests, every existing unit test).

The two protocols (:class:`BatfishRunner`, :class:`BatfishSession`) are
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
- Peak container RSS (Objective 4) is sampled via ``docker stats
  --no-stream`` on a background thread while the inner solve runs; the
  peak is surfaced in ``batfish_stats.json`` as ``peak_rss_mb``. Sampling
  is best-effort — if ``docker stats`` is unavailable on the host the
  field is ``None`` rather than faking a zero.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
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
    "BatfishService",
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
    "isisl1": "isis",
    "isisl2": "isis",
    "isisel1": "isis",
    "isisel2": "isis",
    "isis-el1": "isis",
    "isis-el2": "isis",
    "isis": "isis",
    "rip": "rip",
    "eigrp": "eigrp",
    "eigrp-ex": "eigrp",
    "eigrpex": "eigrp",
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


_SENTINEL_IFACES = {"dynamic", "null_interface", "null0", "null_0", "none"}


def _clean_ip(ip: Any) -> str | None:
    s = _none_or_str(ip)
    if s is None:
        return None
    # Batfish emits "AUTO/NONE(-1l)" for connected routes' pseudo next-hop.
    if s.startswith("AUTO/NONE") or s.lower() == "none":
        return None
    return s


def _clean_iface(iface: Any) -> str | None:
    s = _none_or_str(iface)
    if s is None:
        return None
    if s.lower() in _SENTINEL_IFACES:
        return None
    return s


def _row_to_next_hops(row: dict[str, Any]) -> list[NextHop]:
    """Accept either ``Next_Hop``: dict form or flat ``Next_Hop_IP``/``Next_Hop_Interface``.

    Batfish 2023+ ships the dict shape; older pybatfish versions produce the
    flat columns. Both are tolerated so the transform is stable across
    upgrades. Sentinel values (``AUTO/NONE*``, ``dynamic``, ``null_interface``)
    collapse to ``None`` so they don't poison the head-to-head next-hop diff.
    """
    nh_dict = row.get("Next_Hop")
    if isinstance(nh_dict, dict):
        ip = _clean_ip(_nh_ip(nh_dict))
        iface = _clean_iface(_nh_iface(nh_dict))
        if ip is None and iface is None:
            return []
        return [NextHop(ip=ip, interface=iface)]
    # Flat form. Batfish sometimes emits a single pair, sometimes a list.
    ip_val = row.get("Next_Hop_IP")
    iface_val = row.get("Next_Hop_Interface")
    if isinstance(ip_val, list):
        ips = ip_val
        ifaces = iface_val if isinstance(iface_val, list) else [iface_val] * len(ips)
        out: list[NextHop] = []
        for ip_raw, ifc_raw in zip(ips, ifaces, strict=False):
            ip = _clean_ip(ip_raw)
            ifc = _clean_iface(ifc_raw)
            if ip is None and ifc is None:
                continue
            out.append(NextHop(ip=ip, interface=ifc))
        return out
    ip = _clean_ip(ip_val)
    iface = _clean_iface(iface_val)
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
    """Written alongside the FIB JSON so reports can surface per-topology timing.

    ``simulate_s`` is the inner-solver wall-clock (``query_routes_s +
    query_bgp_s``). It excludes docker startup, JVM / Jetty boot, and
    ``init_snapshot`` upload. ``total_s`` is the outer wall-clock covering
    everything from ``docker run`` through ``docker rm`` — on a persistent
    service this is the per-call wall, *not* the container lifetime.

    ``container_start_s`` is the one-off cost of ``docker run`` +
    :meth:`BatfishRunner.wait_ready` and is attributed in full to the
    first call on a persistent service (``warm == False``) and zero on
    every subsequent call (``warm == True``). The legacy one-shot
    :func:`run_batfish` always reports ``warm == False`` because its
    container lifetime equals one call.

    ``peak_rss_mb`` is the peak RSS of the Batfish container over the
    *solve* window (init_snapshot + query_routes + query_bgp), sampled
    via ``docker stats --no-stream`` on a background thread. ``None`` if
    sampling was skipped or failed; never faked to zero.

    ``peak_rss_source`` is ``"docker-stats"`` in production; ``None`` when
    ``peak_rss_mb`` is ``None``. Symmetric with
    ``HammerheadStats.peak_rss_source`` so the report renderer can key
    off the sampler name uniformly across tools.

    ``peak_rss_sample_count`` is the number of successful ``docker stats
    --no-stream`` readings that fed ``peak_rss_mb``. Zero iff
    ``peak_rss_mb is None``. Used by readers to distinguish a robust
    reading (many samples) from "container died in 50 ms" (1-2 samples).
    """

    topology: str
    started_iso: str
    init_snapshot_s: float
    query_routes_s: float
    query_bgp_s: float
    simulate_s: float
    total_s: float
    warm: bool = False
    container_start_s: float = 0.0
    peak_rss_mb: int | None = None
    peak_rss_source: str | None = None
    peak_rss_sample_count: int = 0

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

    def wait_ready(self, cfg: BatfishConfig, container_id: str) -> None: ...

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

    def wait_ready(self, cfg: BatfishConfig, container_id: str) -> None:  # pragma: no cover - live docker only
        # TCP accept is necessary but not sufficient — Batfish's Jetty binds
        # the port before it starts serving the REST surface. We poll the
        # coordinator's ``/v2/question_templates`` endpoint (the same URL
        # pybatfish hits first) until it returns a 2xx or 401. Anything else
        # (incl. RemoteDisconnected) means the HTTP stack isn't up yet.
        import socket  # noqa: PLC0415
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415
        deadline = time.monotonic() + cfg.startup_timeout_s
        # pybatfish hits the v2 coordinator on ``service_port`` (9996 by
        # default); 9997 is the internal work-manager REST (v1) and doesn't
        # respond to the question_templates URL. Probe the same endpoint
        # pybatfish will use first.
        probe_url = f"http://127.0.0.1:{cfg.service_port}/v2/question_templates?verbose=False"
        while time.monotonic() < deadline:
            # Container exited → fast-fail instead of burning the whole timeout.
            rc, out, _ = self.run_cmd(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_id]
            )
            if rc == 0 and out.strip().lower() == "false":
                logs_rc, logs_out, _ = self.run_cmd(["docker", "logs", container_id])
                tail = "\n".join(logs_out.splitlines()[-20:]) if logs_rc == 0 else ""
                raise RuntimeError(f"Batfish container exited before readiness: {tail}")
            if not self._port_open(cfg.service_port):
                time.sleep(2.0)
                continue
            try:
                req = urllib.request.Request(probe_url, method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:  # noqa: S310
                    if 200 <= resp.status < 500:
                        return
            except urllib.error.HTTPError as exc:
                # 401/403 from Batfish still means the REST surface is live.
                if 400 <= exc.code < 500:
                    return
            except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout):
                pass
            time.sleep(2.0)
        raise TimeoutError(f"Batfish did not start within {cfg.startup_timeout_s}s")

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
        import socket  # noqa: PLC0415
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def stop(self, container_id: str) -> None:
        rc, _, err = self.run_cmd(["docker", "rm", "-f", container_id])
        if rc != 0:
            raise RuntimeError(f"docker rm batfish failed: {err}")


def _docker_run(argv: list[str]) -> tuple[int, str, str]:  # pragma: no cover - thin wrapper
    import subprocess  # noqa: PLC0415 — local import; not needed in tests
    p = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=300)
    return p.returncode, p.stdout, p.stderr


# Batfish has no standalone FRR parser (RANCID tag "frr" maps to UNSUPPORTED
# in VendorConfigurationFormatDetector). The only way to reach Batfish's
# FRR grammar is via CUMULUS_CONCATENATED, triggered by the marker
# `# This file describes the network interfaces`. Without the wrap, our
# raw frr.conf is misdetected as CISCO_IOS (because `interface X` is the
# CISCO_LIKE_PATTERN giveaway); the IOS parser then mis-extracts some
# routes on topologies without `update-source lo` and crashes with a
# NullPointerException on topologies that do have it. Wrap every FRR
# config in a Cumulus-concatenated envelope with a synthesized
# `/etc/network/interfaces` section derived from the frr.conf's own
# interface blocks.
_FRR_VERSION = "8.4.1"


def _looks_like_frr(path: Path) -> bool:
    """True if the first ~500 bytes contain an unmistakable FRR marker."""
    try:
        head = path.read_text(errors="ignore")[:512]
    except OSError:
        return False
    return ("frr defaults" in head) or head.startswith("frr version")


def _wrap_frr_as_cumulus_concatenated(body: str, hostname: str) -> str:
    """Wrap an ``frr.conf`` body so Batfish parses it via CUMULUS_CONCATENATED.

    The returned string follows Batfish's canonical concatenated layout
    (derived from its own ``bgp_neighbor_undefined_routemap`` /
    ``interface_test`` test fixtures):

      1. bare hostname on the first line (no ``# /etc/hostname`` header)
      2. ``# This file describes the network interfaces`` — lexer marker
         that opens the ``/etc/network/interfaces`` section
      3. one ``iface`` stanza per interface mentioned in the FRR config,
         with single-space-indented ``address`` lines; ``lo`` gets
         ``iface lo inet loopback``, everything else bare ``iface X``
      4. ``# ports.conf --`` — lexer marker that opens
         ``/etc/cumulus/ports.conf`` (content may be empty)
      5. ``frr version ...`` — lexer marker that opens
         ``/etc/frr/frr.conf``; the original body follows
    """
    iface_ips: dict[str, list[str]] = {}
    current_iface: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("interface ") and not line.startswith(" "):
            current_iface = stripped.split(None, 1)[1].strip()
            iface_ips.setdefault(current_iface, [])
        elif current_iface is not None and stripped.startswith("ip address "):
            addr = stripped.split(None, 2)[2].strip()
            if addr:
                iface_ips[current_iface].append(addr)
        elif not line.startswith(" ") and stripped and not stripped.startswith("!"):
            current_iface = None

    lines: list[str] = []
    lines.append(hostname)
    lines.append("# This file describes the network interfaces")
    lines.append("")
    # lo first, then the rest sorted for determinism.
    ordered = sorted(iface_ips.keys(), key=lambda n: (n != "lo", n))
    for iface in ordered:
        if iface == "lo":
            lines.append(f"iface {iface} inet loopback")
        else:
            lines.append(f"iface {iface}")
        for addr in iface_ips[iface]:
            lines.append(f" address {addr}")
        lines.append("")
    lines.append("# ports.conf --")
    lines.append("")
    # `frr version ...` is the grammar's delimiter for the frr.conf
    # sub-section; prepend one only if the body doesn't already start
    # with one.
    head = body.splitlines()[:3]
    if not any(l.startswith("frr version") for l in head):
        lines.append(f"frr version {_FRR_VERSION}")
    lines.append(body.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _stage_config(src: Path, dst: Path, kind: str | None) -> None:
    """Copy ``src`` to ``dst``, wrapping FRR configs for Batfish when needed.

    For FRR configs, emit a CUMULUS_CONCATENATED envelope so Batfish
    selects its FRR grammar instead of the Cisco IOS fallback.
    Non-FRR kinds pass through to a plain copy, since their real
    vendor headers already unambiguously identify the format.
    """
    if kind == "frr":
        body = src.read_text()
        hostname = dst.stem
        dst.write_text(_wrap_frr_as_cumulus_concatenated(body, hostname))
        return
    shutil.copyfile(src, dst)


def _stage_snapshot(configs_dir: Path, stage_root: Path) -> None:
    """Stage ``configs_dir`` into Batfish's expected ``<root>/configs`` layout.

    Mixed-vendor topologies drop a per-vendor config in each ``<device>/``
    subdir; we pick the first file that matches our known set and rename
    it ``<device>.cfg``. Non-FRR kinds pass through as-is; FRR configs
    get the :func:`_wrap_frr_as_cumulus_concatenated` envelope so Batfish
    picks its FRR grammar instead of falling back to Cisco IOS.
    """
    stage_cfg_dir = stage_root / "configs"
    stage_cfg_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(configs_dir.iterdir()):
        if child.is_dir():
            picked = None
            picked_kind: str | None = None
            for candidate, kind in (
                ("frr.conf", "frr"),
                ("startup-config", "arista"),
                ("running-config", None),
                ("config.boot", "juniper"),
                ("config", None),
            ):
                p = child / candidate
                if p.is_file():
                    picked = p
                    picked_kind = kind
                    break
            if picked is not None:
                _stage_config(picked, stage_cfg_dir / f"{child.name}.cfg", picked_kind)
                continue
            # AWS describe-* JSON snapshot lives in configs/aws/. Batfish
            # expects aws_configs/ at the snapshot root.
            if child.name == "aws":
                aws_stage = stage_root / "aws_configs"
                aws_stage.mkdir(parents=True, exist_ok=True)
                for aws_file in sorted(child.glob("*.json")):
                    shutil.copyfile(aws_file, aws_stage / aws_file.name)
        elif child.is_file() and child.suffix in {".cfg", ".conf"}:
            # Top-level file (rare): sniff content for FRR markers.
            kind = "frr" if _looks_like_frr(child) else None
            _stage_config(child, stage_cfg_dir / child.name, kind)


class BatfishService:
    """Long-lived Batfish container + pybatfish session.

    Purpose: eliminate the JVM cold-start tax the harness was previously
    paying per trial. Calling :meth:`start` once and then :meth:`run_one`
    N times amortises container start + REST readiness across the whole
    trial loop, so ``warm`` timings reflect the "operator re-runs a
    snapshot on a running Batfish" regime rather than the "spin up a
    fresh container for each snapshot" regime. Both regimes are
    legitimate; we measure both and let the reader choose — see
    README § 2 / § 3 for the framing.

    Lifecycle::

        svc = BatfishService(config=..., runner=..., session_factory=...)
        svc.start()                               # JVM cold-start once
        stats1 = svc.run_one(cfgs_a, out_a, topology="t1")  # warm=False
        stats2 = svc.run_one(cfgs_b, out_b, topology="t2")  # warm=True
        svc.close()

    Or as a context manager::

        with BatfishService() as svc:
            svc.run_one(...)

    Idempotent — ``start()`` on a started service is a no-op; ``close()``
    on a closed service is a no-op. The container is always torn down
    on :meth:`close` (and on ``__exit__``) even when :meth:`run_one`
    raises. Re-entering after ``close()`` is not supported; construct
    a fresh :class:`BatfishService`.
    """

    __slots__ = (
        "_cfg", "_runner", "_session_factory",
        "_container_id", "_session", "_calls", "_container_start_s",
        "_sample_memory",
    )

    def __init__(
        self,
        *,
        config: BatfishConfig | None = None,
        runner: BatfishRunner | None = None,
        session_factory: Callable[[BatfishConfig], BatfishSession] | None = None,
        sample_memory: bool = True,
    ) -> None:
        self._cfg = config or BatfishConfig()
        self._runner = runner or DockerBatfishRunner()
        self._session_factory = session_factory or _default_pybatfish_session_factory
        self._container_id: str | None = None
        self._session: BatfishSession | None = None
        self._calls = 0
        self._container_start_s = 0.0
        self._sample_memory = sample_memory

    @property
    def container_start_s(self) -> float:
        """Cold-start wall-clock (``docker run`` + ``wait_ready``). 0.0 before start."""
        return self._container_start_s

    @property
    def calls(self) -> int:
        """Number of :meth:`run_one` invocations since :meth:`start`."""
        return self._calls

    @property
    def started(self) -> bool:
        return self._container_id is not None

    def start(self) -> None:
        if self._container_id is not None:
            return
        t0 = time.monotonic()
        self._container_id = self._runner.start(self._cfg)
        try:
            self._runner.wait_ready(self._cfg, self._container_id)
            self._session = self._session_factory(self._cfg)
        except Exception:
            # Failed to reach ready — tear down the half-spawned container
            # so we don't leak a running JVM on the operator's host.
            try:
                self._runner.stop(self._container_id)
            except Exception as exc:  # noqa: BLE001 — stop failure is non-fatal; logged
                log.warning("batfish: container stop failed during abort: %s", exc)
            self._container_id = None
            self._session = None
            raise
        self._container_start_s = time.monotonic() - t0

    def close(self) -> None:
        if self._container_id is None:
            return
        try:
            self._runner.stop(self._container_id)
        except Exception as exc:  # noqa: BLE001 — stop failure is non-fatal; logged
            log.warning("batfish: container stop failed: %s", exc)
        self._container_id = None
        self._session = None

    def __enter__(self) -> BatfishService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def run_one(
        self,
        configs_dir: Path,
        out_dir: Path,
        *,
        topology: str,
    ) -> BatfishStats:
        """Upload a snapshot, query routes + bgpRib, write per-(node,vrf) FIBs.

        Returns a :class:`BatfishStats` whose ``warm`` is ``False`` on the
        very first call since :meth:`start` (and carries the full
        ``container_start_s`` as a startup-cost breadcrumb) and ``True``
        thereafter. ``total_s`` is always the per-call wall-clock, never
        the container lifetime.
        """
        if self._container_id is None or self._session is None:
            self.start()
        # Re-bind after start() so mypy/pyright see the non-None guarantee.
        assert self._container_id is not None and self._session is not None

        started_iso = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        t0 = time.monotonic()

        from harness.peak_rss import DockerStatsSampler, peak_rss_enabled  # noqa: PLC0415

        sampler: DockerStatsSampler | None = None
        if self._sample_memory and peak_rss_enabled():
            try:
                sampler = DockerStatsSampler(container_id=self._container_id)
                sampler.start()
            except Exception as exc:  # noqa: BLE001 — sampler is best-effort
                log.warning("batfish: memory sampler start failed: %s", exc)
                sampler = None

        try:
            t_init = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="bf-snap-") as stage_root_str:
                stage_root = Path(stage_root_str)
                _stage_snapshot(configs_dir, stage_root)
                self._session.init_snapshot(
                    str(stage_root), name=f"bench-{topology}", overwrite=True
                )
            init_s = time.monotonic() - t_init

            t_routes = time.monotonic()
            route_rows = self._session.get_routes()
            routes_s = time.monotonic() - t_routes

            t_bgp = time.monotonic()
            bgp_rows = self._session.get_bgp_rib()
            bgp_s = time.monotonic() - t_bgp

            fibs = transform_batfish_rows(route_rows, bgp_rows=bgp_rows)
            out_dir.mkdir(parents=True, exist_ok=True)
            for fib in fibs:
                out_path = out_dir / f"{fib.node}__{fib.vrf}.json"
                out_path.write_text(fib.model_dump_json(indent=2) + "\n")
        finally:
            if sampler is not None:
                reading = sampler.stop()
                peak_mb = reading.mb
                peak_source = reading.source if reading.mb is not None else None
                peak_samples = reading.sample_count
            else:
                peak_mb = None
                peak_source = None
                peak_samples = 0

        warm = self._calls > 0
        self._calls += 1
        stats = BatfishStats(
            topology=topology,
            started_iso=started_iso,
            init_snapshot_s=init_s,
            query_routes_s=routes_s,
            query_bgp_s=bgp_s,
            simulate_s=routes_s + bgp_s,
            total_s=time.monotonic() - t0,
            warm=warm,
            # Attribute the full cold-start cost to the first call; zero
            # it out thereafter so summing per-call stats across trials
            # doesn't double-count.
            container_start_s=0.0 if warm else self._container_start_s,
            peak_rss_mb=peak_mb,
            peak_rss_source=peak_source,
            peak_rss_sample_count=peak_samples,
        )
        (out_dir / "batfish_stats.json").write_text(
            json.dumps(stats.as_dict(), indent=2) + "\n"
        )
        return stats


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

    Thin one-shot wrapper around :class:`BatfishService`. Equivalent to
    ``with BatfishService(...) as svc: return svc.run_one(...)``. Kept
    verbatim as a backward-compatible entrypoint for the 3-way truth
    path and one-off smoke tests. The ``warm`` field on the returned
    :class:`BatfishStats` is always ``False`` here — the container
    lifetime equals the call.

    ``session_factory`` and ``runner`` are test seams. Production code
    leaves both ``None`` and a default pybatfish-backed session is used.

    Returns a :class:`BatfishStats` with per-phase timing. Raises
    ``RuntimeError`` / ``TimeoutError`` on any underlying failure; the
    container is always stopped on exit.
    """
    with BatfishService(
        config=config,
        runner=runner,
        session_factory=session_factory,
        # The one-shot path never samples memory — adds a background
        # thread for no gain when the container is torn down instantly.
        # Persistent-service callers opt in via BatfishService directly.
        sample_memory=False,
    ) as svc:
        return svc.run_one(configs_dir, out_dir, topology=topology)


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
