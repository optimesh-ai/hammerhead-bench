"""Hammerhead CLI wrapper — Phase 6 deliverable.

Shells out to ``$HAMMERHEAD_CLI`` twice per topology:

1. ``hammerhead simulate <configs_dir> --format json`` — returns a
   :py:mod:`SimulateSummaryJson`-shaped object with a ``devices[]`` list
   of hostnames. Used to discover which devices to query.
2. ``hammerhead rib --config-dir <configs_dir> --device <X> --format json``
   — returns one device's full RIB (all VRFs flattened).

Each device response is routed through
:func:`harness.tools.hammerhead_transform.transform_rib_view` to produce
canonical :class:`NodeFib` rows, written to
``<out_dir>/<hostname>__default.json``.

Test seams: :class:`HammerheadRunner` is a Protocol injected via the
``runner`` keyword argument. Production default is
:class:`SubprocessHammerheadRunner`, which shells out to
``$HAMMERHEAD_CLI`` with a 300s timeout. Unit tests inject a fake runner
that returns canned JSON so the orchestration path (writing per-device
files, stats, error handling) is exercisable without a real binary.

Memory discipline:

- The harness holds the host memory guard. This wrapper adds nothing new
  — Hammerhead is a short-lived subprocess that inherits the harness
  RLIMIT_AS.
- No container is spawned; nothing to tear down. Wall-time stats are
  captured for the report layer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.tools.hammerhead_transform import transform_rib_view

__all__ = [
    "HammerheadConfig",
    "HammerheadRunner",
    "HammerheadStats",
    "SubprocessHammerheadRunner",
    "run_hammerhead",
]

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True, slots=True)
class HammerheadConfig:
    """Tunables for the CLI wrapper. All have sensible defaults."""

    hammerhead_cli: str = "hammerhead"
    timeout_s: float = _DEFAULT_TIMEOUT_S


@dataclass(slots=True)
class HammerheadStats:
    """Timing + result stats for one topology run.

    Written alongside the per-device NodeFib JSON so the report layer
    can render a per-topology table without re-parsing anything.
    """

    topology: str
    started_iso: str
    simulate_s: float
    rib_total_s: float
    device_count: int
    total_routes: int
    total_s: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HammerheadRunner(Protocol):
    """Test seam for the two shell-outs the wrapper makes.

    Default = :class:`SubprocessHammerheadRunner`. Unit tests inject a
    fake that returns canned JSON so the orchestration path runs
    without a real binary on the host.
    """

    def simulate(self, cfg: HammerheadConfig, configs_dir: Path) -> dict[str, Any]:
        """Run ``simulate`` and return the parsed JSON."""

    def rib(
        self, cfg: HammerheadConfig, configs_dir: Path, device: str
    ) -> dict[str, Any]:
        """Run ``rib --device <device>`` and return the parsed JSON."""


@dataclass(slots=True)
class SubprocessHammerheadRunner:
    """Production runner: shells out to ``$HAMMERHEAD_CLI`` via subprocess.

    The run function is injected so tests can swap it for a fake that
    returns canned stdout without spawning a subprocess. The default
    shells out to :func:`subprocess.run` with a timeout.
    """

    run_cmd: Callable[[list[str], float], tuple[int, str, str]] = field(
        default_factory=lambda: _subprocess_run
    )

    def simulate(self, cfg: HammerheadConfig, configs_dir: Path) -> dict[str, Any]:
        rc, out, err = self.run_cmd(
            [cfg.hammerhead_cli, "simulate", str(configs_dir), "--format", "json"],
            cfg.timeout_s,
        )
        if rc != 0:
            raise RuntimeError(
                f"hammerhead simulate failed (rc={rc}): {err or out}"
            )
        return _parse_json(out, origin="hammerhead simulate")

    def rib(
        self, cfg: HammerheadConfig, configs_dir: Path, device: str
    ) -> dict[str, Any]:
        rc, out, err = self.run_cmd(
            [
                cfg.hammerhead_cli,
                "rib",
                "--device",
                device,
                "--format",
                "json",
                str(configs_dir),
            ],
            cfg.timeout_s,
        )
        if rc != 0:
            raise RuntimeError(
                f"hammerhead rib {device} failed (rc={rc}): {err or out}"
            )
        return _parse_json(out, origin=f"hammerhead rib {device}")


def _subprocess_run(cmd: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Default ``run_cmd`` — shells out with a timeout.

    Returns ``(returncode, stdout, stderr)``. A timeout surfaces as
    rc=124, matching the `timeout` utility's convention.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is explicit, no shell.
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or ""
    return proc.returncode, proc.stdout, proc.stderr


def _parse_json(raw: str, *, origin: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{origin}: invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"{origin}: expected JSON object, got {type(obj).__name__}")
    return obj


# ---- orchestrator --------------------------------------------------------


def run_hammerhead(
    configs_dir: Path,
    out_dir: Path,
    *,
    topology: str,
    runner: HammerheadRunner | None = None,
    config: HammerheadConfig | None = None,
) -> HammerheadStats:
    """Run Hammerhead against ``configs_dir``, write canonical NodeFibs.

    Output layout:

    - ``<out_dir>/<hostname>__default.json`` — one NodeFib per device
    - ``<out_dir>/hammerhead_stats.json`` — timing + route counts

    Raises ``RuntimeError`` if the CLI binary can't be found or returns
    non-zero. The caller's memory guard is responsible for any host-level
    cap.
    """
    cfg = config or HammerheadConfig()
    rn: HammerheadRunner = runner or SubprocessHammerheadRunner()

    # Fail fast if the binary doesn't exist — subprocess.run's ENOENT
    # message is cryptic. Only checked on the default runner path; when
    # a fake is injected we trust it.
    if runner is None and shutil.which(cfg.hammerhead_cli) is None:
        raise RuntimeError(
            f"hammerhead CLI not found on PATH as {cfg.hammerhead_cli!r}; "
            "set HAMMERHEAD_CLI or add the binary to PATH"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.monotonic()

    t_sim = time.monotonic()
    sim_view = rn.simulate(cfg, configs_dir)
    sim_s = time.monotonic() - t_sim

    devices = _device_hostnames(sim_view)
    log.info("hammerhead: %s -> %d devices", topology, len(devices))

    t_rib = time.monotonic()
    total_routes = 0
    for hostname in devices:
        view = rn.rib(cfg, configs_dir, hostname)
        fib = transform_rib_view(view)
        out_path = out_dir / f"{fib.node}__{fib.vrf}.json"
        out_path.write_text(fib.model_dump_json(indent=2) + "\n")
        total_routes += len(fib.routes)
    rib_s = time.monotonic() - t_rib

    stats = HammerheadStats(
        topology=topology,
        started_iso=started_iso,
        simulate_s=sim_s,
        rib_total_s=rib_s,
        device_count=len(devices),
        total_routes=total_routes,
        total_s=time.monotonic() - t0,
    )
    (out_dir / "hammerhead_stats.json").write_text(
        json.dumps(stats.as_dict(), indent=2) + "\n"
    )
    return stats


def _device_hostnames(sim_view: dict[str, Any]) -> list[str]:
    """Extract hostnames from a simulate JSON response.

    Shape: ``{..., "devices": [{"hostname": "r1", ...}, ...]}``.
    Returns a sorted list so iteration is deterministic.
    """
    raw = sim_view.get("devices")
    if not isinstance(raw, list):
        return []
    hostnames: set[str] = set()
    for d in raw:
        if not isinstance(d, dict):
            continue
        hn = d.get("hostname")
        if isinstance(hn, str) and hn.strip():
            hostnames.add(hn.strip())
    return sorted(hostnames)


# Environment discovery helper. Used by callers that want to surface
# the resolved binary path in manifest JSON.
def resolve_hammerhead_cli(override: str | None = None) -> str:
    """Return the hammerhead CLI path to use.

    Precedence: ``override`` > ``$HAMMERHEAD_CLI`` > ``hammerhead`` on PATH.
    """
    if override:
        return override
    env = os.environ.get("HAMMERHEAD_CLI")
    if env:
        return env
    return "hammerhead"
