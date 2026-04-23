"""Hammerhead CLI wrapper — bulk ``simulate --emit-rib all`` path.

Shells out to ``$HAMMERHEAD_CLI`` **once** per topology (post Hammerhead
commit b46eb45 / 2026-04-22):

    hammerhead simulate <configs_dir> --emit-rib all --format json

The response is a single JSON object shaped as::

    {
      "rib": {
        "<hostname-A>": {"hostname": "<hostname-A>", "entries": [...]},
        "<hostname-B>": {"hostname": "<hostname-B>", "entries": [...]},
        ...
      }
    }

Each device view is routed through
:func:`harness.tools.hammerhead_transform.transform_rib_view` to produce
canonical :class:`NodeFib` rows, written to
``<out_dir>/<hostname>__default.json``.

Historical note — **per-device rib loop, removed 2026-04-22:**
Before Hammerhead commit b46eb45 landed the bulk emit-rib API, the
harness shelled out ``hammerhead simulate`` once for device discovery
and then looped ``hammerhead rib --device <X>`` per device (N+1
subprocesses per topology). Each of those extra invocations rebuilt the
full Pipeline from scratch (re-parse, re-simulate every protocol) so
the per-device rib phase dominated Hammerhead's measured wall-clock on
larger topologies. Switching to a single bulk emit collapses that N+1
subprocess tax into one invocation that runs ``Pipeline::build``
exactly once (enforced by an integration test in the Hammerhead repo:
``crates/hammerhead-cli/tests/pipeline_build_count.rs``), which is why
post-migration numbers are dramatically faster than the pre-migration
corpus.

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
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.peak_rss import PeakRssReading, peak_rss_enabled, rusage_peak_mb
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

    Field semantics (post bulk-emit migration, 2026-04-22):

    * ``simulate_s`` — wall-clock of the single
      ``hammerhead simulate --emit-rib all`` subprocess. Covers solver
      work **and** per-device RIB materialization in one shot (the
      bulk emit amortises ``Pipeline::build`` across every device).
    * ``rib_total_s`` — legacy field, **always 0.0** in the bulk path.
      Retained for results-JSON schema backward compatibility so
      downstream consumers (report layer, Batfish comparison glue) do
      not need to branch on pre- vs post-migration shape. ``fair_ratio``
      still sums ``simulate_s + rib_total_s`` in its denominator; with
      ``rib_total_s == 0`` it collapses to ``batfish_simulate_s /
      simulate_s`` — i.e. ``fair_ratio`` and ``asym_ratio`` converge
      post-migration. See ``pipeline.py::ASYM_RATIO_NOTE`` and
      README § 2 for the formal discussion.
    * ``peak_rss_mb`` — peak resident-set size of the
      ``hammerhead simulate`` subprocess in MB, sampled via
      ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` across the
      ``rn.simulate_emit_rib_all`` window. ``None`` when sampling was
      disabled (``HAMMERHEAD_BENCH_DISABLE_PEAK_RSS=1``) or the
      reading was unavailable (Windows, zero-rusage). Symmetric with
      ``BatfishStats.peak_rss_mb``.
    * ``peak_rss_source`` — ``"rusage"`` in production; ``None`` when
      ``peak_rss_mb`` is ``None``. Propagated into reports so readers
      see which sampler produced the number.
    * ``peak_rss_sample_count`` — number of successful readings that
      fed ``peak_rss_mb``. For ``rusage`` this is 1 (one pre/post
      pair); for the docker-stats sampler on the Batfish side it's
      the poll count. Zero iff ``peak_rss_mb is None``.
    """

    topology: str
    started_iso: str
    simulate_s: float
    rib_total_s: float
    device_count: int
    total_routes: int
    total_s: float
    peak_rss_mb: int | None = None
    peak_rss_source: str | None = None
    peak_rss_sample_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HammerheadRunner(Protocol):
    """Test seam for the one shell-out the wrapper makes.

    Default = :class:`SubprocessHammerheadRunner`. Unit tests inject a
    fake that returns canned JSON so the orchestration path runs
    without a real binary on the host.
    """

    def simulate_emit_rib_all(
        self, cfg: HammerheadConfig, configs_dir: Path
    ) -> dict[str, Any]:
        """Run ``simulate --emit-rib all`` and return the parsed JSON.

        Expected shape::

            {"rib": {"<hostname>": {"hostname": "...", "entries": [...]}}}
        """


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

    def simulate_emit_rib_all(
        self, cfg: HammerheadConfig, configs_dir: Path
    ) -> dict[str, Any]:
        rc, out, err = self.run_cmd(
            [
                cfg.hammerhead_cli,
                "simulate",
                str(configs_dir),
                "--emit-rib",
                "all",
                "--format",
                "json",
            ],
            cfg.timeout_s,
        )
        if rc != 0:
            raise RuntimeError(
                f"hammerhead simulate --emit-rib all failed (rc={rc}): {err or out}"
            )
        return _parse_json(out, origin="hammerhead simulate --emit-rib all")


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
    expected_hostnames: Iterable[str] | None = None,
) -> HammerheadStats:
    """Run Hammerhead against ``configs_dir``, write canonical NodeFibs.

    Output layout:

    - ``<out_dir>/<hostname>__default.json`` — one NodeFib per device
    - ``<out_dir>/hammerhead_stats.json`` — timing + route counts

    Raises ``RuntimeError`` if the CLI binary can't be found, returns
    non-zero, emits malformed JSON, or — when ``expected_hostnames`` is
    supplied — if the bulk RIB response is missing any of those hosts.
    The caller's memory guard is responsible for any host-level cap.

    ``expected_hostnames`` is the topology's full node list (passed by
    the default hook, which loads the ``TopologySpec``). We assert
    every expected host is a key in the response so a silent
    dropped-device bug in the Hammerhead pipeline surfaces loudly as a
    bench failure rather than a coverage regression.
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
    if peak_rss_enabled():
        body_result, peak_reading = rusage_peak_mb(
            lambda: rn.simulate_emit_rib_all(cfg, configs_dir),
            source="rusage",
        )
        bulk = body_result  # type: ignore[assignment]
    else:
        bulk = rn.simulate_emit_rib_all(cfg, configs_dir)
        peak_reading = PeakRssReading(mb=None, sample_count=0, source="rusage")
    sim_s = time.monotonic() - t_sim

    rib_map = _extract_rib_map(bulk)

    if expected_hostnames is not None:
        expected = {h.strip() for h in expected_hostnames if h and h.strip()}
        got = set(rib_map.keys())
        missing = sorted(expected - got)
        if missing:
            raise RuntimeError(
                "hammerhead simulate --emit-rib all: response missing "
                f"expected device(s) for topology {topology!r}: {missing}; "
                f"got {sorted(got)}"
            )

    hostnames = sorted(rib_map.keys())
    log.info(
        "hammerhead: %s -> %d devices (bulk emit-rib)", topology, len(hostnames)
    )

    total_routes = 0
    for hostname in hostnames:
        view = rib_map[hostname]
        fib = transform_rib_view(view)
        out_path = out_dir / f"{fib.node}__{fib.vrf}.json"
        out_path.write_text(fib.model_dump_json(indent=2) + "\n")
        total_routes += len(fib.routes)

    stats = HammerheadStats(
        topology=topology,
        started_iso=started_iso,
        simulate_s=sim_s,
        # Bulk emit path — no separate rib phase. Field retained for
        # results-JSON schema stability; see HammerheadStats docstring.
        rib_total_s=0.0,
        device_count=len(hostnames),
        total_routes=total_routes,
        total_s=time.monotonic() - t0,
        peak_rss_mb=peak_reading.mb,
        peak_rss_source=peak_reading.source if peak_reading.mb is not None else None,
        peak_rss_sample_count=peak_reading.sample_count,
    )
    (out_dir / "hammerhead_stats.json").write_text(
        json.dumps(stats.as_dict(), indent=2) + "\n"
    )
    return stats


def _extract_rib_map(bulk: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract ``{hostname: view}`` from the bulk emit-rib response.

    Shape::

        {"rib": {"<hostname>": {"hostname": "...", "entries": [...]}}}

    Returns a ``dict`` keyed by (trimmed, non-empty) hostname. Entries
    whose top-level key disagrees with the nested ``hostname`` field,
    or whose value is not an object, are dropped silently — the
    caller's ``expected_hostnames`` assertion is the place that turns
    a missing device into a loud failure.
    """
    rib = bulk.get("rib")
    if not isinstance(rib, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for k, v in rib.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if not isinstance(v, dict):
            continue
        # Prefer the nested hostname when present; fall back to the
        # top-level key. The Hammerhead CLI emits both; guard against
        # drift where one side is dropped.
        nested = v.get("hostname")
        host = (nested if isinstance(nested, str) and nested.strip() else k).strip()
        out[host] = v
    return out


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
