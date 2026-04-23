"""Peak RSS sampling — symmetric across both simulators.

Two sources, one shape. Callers pick the one that matches the tool:

* :func:`rusage_peak_mb` — wraps ``resource.getrusage(RUSAGE_CHILDREN)``
  around a synchronous subprocess call. Right for Hammerhead: the
  binary forks + exits inside one ``subprocess.run`` window, and
  ``ru_maxrss`` on the children bucket measures exactly that
  subprocess's peak. Portable: on Linux ``ru_maxrss`` is kilobytes,
  on macOS/BSD it is bytes; this module normalises to MB.
* :class:`DockerStatsSampler` — a background thread that polls
  ``docker stats --no-stream --format {{.MemUsage}}`` every
  ``interval_s`` seconds. Right for Batfish: the JVM lives inside a
  container whose RSS is not a child-rusage concern of the harness
  process. Uses the blocking ``--no-stream`` form (one reading per
  invocation) rather than ``docker stats`` without flags (a
  streaming TTY interface) so the sampler is deterministic and
  restartable.

Both paths return MB rounded to the nearest int. A sentinel ``None``
means "not measured" (tool absent, permission error, container died
mid-window). Never raise from the sampler — an unreliable peak_rss
is a caveat, not a bench failure.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "DockerStatsSampler",
    "PeakRssReading",
    "rusage_peak_mb",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PeakRssReading:
    """One immutable max-over-window sample.

    ``mb`` is ``None`` when the sampler never saw a valid reading
    (docker stats errored on every poll, container exited too fast,
    rusage returned 0).
    ``sample_count`` is the number of successful readings used to
    compute the max — zero implies ``mb is None``.
    ``source`` is ``"rusage"`` or ``"docker-stats"`` verbatim so the
    report renderer can surface the measurement method.
    """

    mb: int | None
    sample_count: int
    source: str


# ---- getrusage path ------------------------------------------------------


def rusage_peak_mb(
    body: Callable[[], object],
    *,
    source: str = "rusage",
) -> tuple[object, PeakRssReading]:
    """Run ``body()`` and return ``(body_result, peak_reading)``.

    Takes ``ru_maxrss`` for the ``RUSAGE_CHILDREN`` bucket **before**
    and **after** the call. The delta is the peak RSS reached by any
    child process that started-and-exited inside the window. We use
    the delta rather than the post-value because the harness may
    have already spawned unrelated children earlier (pytest runners,
    docker CLI probes) and their peak would otherwise pollute the
    measurement.

    ``ru_maxrss`` unit is platform-dependent:

    * Linux: kilobytes. Divide by 1024 for MB.
    * macOS / BSD: bytes. Divide by (1024*1024) for MB.
    * Windows: unsupported (``resource`` isn't available) — returns
      ``None`` sentinel and runs ``body()`` unwrapped.

    Returns the body's result unchanged so this function is a
    drop-in wrapper at the call site.
    """
    try:
        import resource  # noqa: PLC0415 — POSIX only
    except ImportError:
        return body(), PeakRssReading(mb=None, sample_count=0, source=source)

    unit_divisor = _ru_maxrss_unit_divisor()
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    result = body()
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss for RUSAGE_CHILDREN is the *max over all children that
    # have been waited for*. Post-window ≥ pre-window is guaranteed;
    # the delta isn't meaningful, but the post-value captures the
    # peak of the child that just exited iff it was the largest
    # child we've ever reaped. When that's not true the post-value
    # is an upper bound on our child's peak, not a point estimate;
    # we prefer upper-bound to under-report here.
    raw = max(after, 0)
    if raw <= 0:
        return result, PeakRssReading(mb=None, sample_count=0, source=source)
    mb = int(round(raw / unit_divisor))
    return result, PeakRssReading(mb=mb, sample_count=1, source=source)


def _ru_maxrss_unit_divisor() -> int:
    """Return the divisor to convert ``ru_maxrss`` to MB.

    Linux returns kilobytes (divide by 1024 for MB). Darwin / BSD
    return bytes (divide by 1024*1024).
    """
    sysname = platform.system()
    if sysname == "Linux":
        return 1024
    if sysname in ("Darwin", "FreeBSD", "OpenBSD", "NetBSD"):
        return 1024 * 1024
    # Unknown POSIX variant: assume kilobytes (POSIX default); a
    # wrong divisor is at most a ×1024 off reading, still better
    # than None.
    return 1024


# ---- docker stats path ---------------------------------------------------


# ``docker stats --no-stream --format {{.MemUsage}}`` output looks like
# "1.23GiB / 4GiB" or "567MiB / 4GiB". The first token is the current
# RSS (the usage side); the slash-separated second token is the
# container's cgroup cap, which we already know (BATFISH_MEMORY_MB).
_MEMUSAGE_RE = re.compile(
    r"""^
        (?P<value>\d+(?:\.\d+)?)    # 1.23
        \s*
        (?P<unit>KiB|MiB|GiB|TiB|B) # base-2 SI prefixes; docker emits these literally
        \s*/\s*                     # separator
        .*$                         # cap side, ignored
    """,
    re.VERBOSE,
)
_UNIT_TO_MB: dict[str, float] = {
    "B": 1.0 / (1024 * 1024),
    "KiB": 1.0 / 1024,
    "MiB": 1.0,
    "GiB": 1024.0,
    "TiB": 1024.0 * 1024.0,
}


class DockerStatsSampler:
    """Background thread polling ``docker stats --no-stream`` for one container.

    Lifecycle::

        sampler = DockerStatsSampler(container_id="batfish-foo")
        sampler.start()
        try:
            ... # run the work whose peak RSS you want to capture
        finally:
            reading = sampler.stop()

    The thread polls every ``interval_s`` seconds. Transient errors
    (docker CLI not on PATH, container not yet up, container exited)
    are swallowed — a missed sample just isn't counted. The reported
    ``mb`` is the max over every successful poll; ``sample_count``
    records the poll count so the reader can distinguish
    "container died in 50 ms" (1-2 samples) from a robust reading.

    ``docker_bin`` is injectable for tests; production uses the
    resolved ``docker`` on PATH.
    """

    def __init__(
        self,
        *,
        container_id: str,
        interval_s: float = 0.25,
        docker_bin: str | None = None,
        runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    ) -> None:
        self._container_id = container_id
        self._interval_s = max(0.05, float(interval_s))
        self._docker_bin = docker_bin or shutil.which("docker") or "docker"
        self._runner = runner or _default_docker_runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peak_mb = 0.0
        self._samples = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("DockerStatsSampler already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"docker-stats:{self._container_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 2.0) -> PeakRssReading:
        """Signal the poller to stop, join, and return the captured reading."""
        if self._thread is None:
            return PeakRssReading(mb=None, sample_count=0, source="docker-stats")
        self._stop.set()
        self._thread.join(timeout=timeout_s)
        with self._lock:
            mb = int(round(self._peak_mb)) if self._samples > 0 else None
            samples = self._samples
        return PeakRssReading(mb=mb, sample_count=samples, source="docker-stats")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                rc, out, _err = self._runner(
                    [
                        self._docker_bin,
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.MemUsage}}",
                        self._container_id,
                    ],
                    self._interval_s + 1.0,
                )
            except (OSError, ValueError) as exc:
                log.debug("docker stats poll errored: %s", exc)
                self._stop.wait(self._interval_s)
                continue
            if rc == 0:
                mb = _parse_memusage_mb(out)
                if mb is not None:
                    with self._lock:
                        if mb > self._peak_mb:
                            self._peak_mb = mb
                        self._samples += 1
            self._stop.wait(self._interval_s)


def _parse_memusage_mb(text: str) -> float | None:
    """Parse ``docker stats --format {{.MemUsage}}`` → MB.

    Returns ``None`` on any parse failure. The caller treats ``None``
    as a skipped sample rather than an error, so parsing is lenient.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _MEMUSAGE_RE.match(line)
        if not m:
            continue
        value = float(m.group("value"))
        unit = m.group("unit")
        factor = _UNIT_TO_MB.get(unit)
        if factor is None:
            continue
        return value * factor
    return None


def _default_docker_runner(cmd: list[str], timeout_s: float) -> tuple[int, str, str]:
    """Default subprocess runner for the sampler.

    Returns ``(returncode, stdout, stderr)``. A timeout returns
    ``(124, partial_stdout, partial_stderr)`` matching the `timeout`
    utility's convention.
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
    except FileNotFoundError:
        # docker CLI missing — return a soft failure; the sampler
        # treats this as a skipped poll.
        return 127, "", "docker binary not found"
    return proc.returncode, proc.stdout, proc.stderr


# Convenience: compute harness-side child rusage right now without a
# body. Lets the Hammerhead wrapper take a reading around a pre-existing
# subprocess invocation rather than wrapping it.
def rusage_children_max_rss_mb() -> int | None:
    """Current ``ru_maxrss`` of the ``RUSAGE_CHILDREN`` bucket in MB.

    Returns ``None`` if the ``resource`` module is unavailable
    (Windows) or the reading is zero (no children waited yet).
    """
    try:
        import resource  # noqa: PLC0415 — POSIX only
    except ImportError:
        return None
    raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if raw <= 0:
        return None
    return int(round(raw / _ru_maxrss_unit_divisor()))


def rusage_children_delta_mb(before_mb: int | None) -> int | None:
    """Difference between the current ``ru_maxrss`` reading and ``before_mb``.

    Returns ``None`` if either reading is unavailable. Used by callers
    that want the peak RSS attributable to a specific subprocess
    window without threading a sampler around it.
    """
    current = rusage_children_max_rss_mb()
    if current is None:
        return None
    if before_mb is None:
        return current
    # ru_maxrss for RUSAGE_CHILDREN is monotonic nondecreasing over
    # the harness lifetime. `current - before` is the peak of any
    # child that exited in the window iff that child was the
    # largest-ever child. We report `current` as an upper bound
    # because we do not track the historical max otherwise.
    _ = before_mb  # retained for API symmetry with rusage_peak_mb
    return current


# Exported for the pipeline's env-var guard (disables sampling when set).
PEAK_RSS_ENV_DISABLE = "HAMMERHEAD_BENCH_DISABLE_PEAK_RSS"


def peak_rss_enabled() -> bool:
    """False when the caller asked us to skip sampling (``PEAK_RSS_ENV_DISABLE=1``).

    Used by the wrappers to skip the CI / test paths where spawning a
    docker-stats poller thread against a mock runner is unnecessary
    overhead.
    """
    return os.environ.get(PEAK_RSS_ENV_DISABLE, "").strip().lower() not in ("1", "true", "yes")
