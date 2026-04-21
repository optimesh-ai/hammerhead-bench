"""Memory guards — the #1 correctness constraint of this harness.

Rules:

- One topology deployed at a time.
- Before deploying a topology, host available RAM must exceed
  ``MEMORY_HEADROOM_MULTIPLIER * sum(container caps)``; default 2.0.
- On Linux the harness process sets ``RLIMIT_AS = 8 GiB``. On macOS this is
  skipped with a warning (setrlimit behaves erratically on Darwin).
- After each teardown, host memory must return within ``BASELINE_SLACK_MB``
  (default 500 MB) of the pre-topology baseline within ``RECOVERY_TIMEOUT_S``
  (default 30 s). If not, the whole run aborts with a dangling-resource report.
- Memory samples are written to ``results/memory.jsonl`` as one JSON object
  per phase per topology with fields documented in :class:`MemorySample`.

Guards are intentionally load-bearing and crash loudly. Do NOT catch-and-swallow
from this module in calling code — let the ``MemoryGuardError`` propagate.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

RLIMIT_AS_BYTES = 8 * 1024**3  # 8 GiB; Linux only.
BASELINE_SLACK_MB = 500
RECOVERY_TIMEOUT_S = 30
DEFAULT_HEADROOM_MULTIPLIER = 2.0

# Phase constants; emit these verbatim so downstream log parsers have stable keys.
PHASE_PRE_DEPLOY = "pre-deploy"
PHASE_POST_DEPLOY = "post-deploy"
PHASE_POST_TEARDOWN = "post-teardown"
PHASE_RECOVERED = "recovered"
VALID_PHASES = frozenset(
    {PHASE_PRE_DEPLOY, PHASE_POST_DEPLOY, PHASE_POST_TEARDOWN, PHASE_RECOVERED}
)


class MemoryGuardError(RuntimeError):
    """Raised when a memory invariant is violated. Never caught inside harness."""


@dataclass(frozen=True, slots=True)
class MemorySample:
    """One row of ``results/memory.jsonl``; emitted per (topology, phase) pair."""

    topology: str
    phase: str  # one of VALID_PHASES
    host_available_mb: int
    rss_harness_mb: int
    sum_container_limits_mb: int
    timestamp_iso: str

    def __post_init__(self) -> None:
        if self.phase not in VALID_PHASES:
            raise ValueError(
                f"MemorySample.phase must be in {sorted(VALID_PHASES)}, got {self.phase!r}"
            )


# ----- rlimit --------------------------------------------------------------


def guard_preflight_rlimit(limit_bytes: int = RLIMIT_AS_BYTES) -> None:
    """Apply ``RLIMIT_AS`` on Linux; warn + no-op elsewhere.

    Raises ``MemoryGuardError`` only if the platform is Linux and the setrlimit
    call fails. On macOS / Darwin the call is skipped with a warning because
    Darwin's ``RLIMIT_AS`` is not authoritative (Mach VM maps escape it).
    """
    if platform.system() != "Linux":
        log.warning(
            "guard_preflight_rlimit: skipping RLIMIT_AS on %s (Darwin/BSD unreliable)",
            platform.system(),
        )
        return
    try:
        import resource  # noqa: PLC0415 — stdlib, Linux-only
    except ImportError as e:  # pragma: no cover — Linux always has `resource`
        raise MemoryGuardError(f"import resource failed on Linux: {e}") from e
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
    except OSError as e:
        raise MemoryGuardError(f"getrlimit(RLIMIT_AS) failed: {e}") from e
    # Don't raise the soft limit above the hard limit (unprivileged processes
    # can only lower the hard limit, not raise it).
    new_soft = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
    new_hard = hard
    try:
        resource.setrlimit(resource.RLIMIT_AS, (new_soft, new_hard))
    except (OSError, ValueError) as e:
        raise MemoryGuardError(
            f"setrlimit(RLIMIT_AS, ({new_soft}, {new_hard})) failed: {e}"
        ) from e
    log.info("RLIMIT_AS set: soft=%d hard=%d", new_soft, new_hard)


# ----- headroom + baseline checks ------------------------------------------


def _available_mb() -> int:
    return int(psutil.virtual_memory().available / (1024**2))


def _rss_mb() -> int:
    return int(psutil.Process(os.getpid()).memory_info().rss / (1024**2))


def sample_memory(
    *,
    topology: str,
    phase: str,
    sum_container_limits_mb: int,
) -> MemorySample:
    """Capture one ``MemorySample`` at the current instant."""
    return MemorySample(
        topology=topology,
        phase=phase,
        host_available_mb=_available_mb(),
        rss_harness_mb=_rss_mb(),
        sum_container_limits_mb=sum_container_limits_mb,
        timestamp_iso=datetime.now(tz=UTC).isoformat(timespec="seconds"),
    )


def append_memory_sample(path: Path, sample: MemorySample) -> None:
    """Append one JSON object per line to ``path``; creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(sample), sort_keys=True) + "\n")


def check_headroom_before_deploy(
    sum_container_limits_mb: int,
    multiplier: float = DEFAULT_HEADROOM_MULTIPLIER,
    *,
    available_mb: int | None = None,
) -> None:
    """Raise :class:`MemoryGuardError` if host RAM is below ``multiplier * cap``.

    ``available_mb`` is a test seam. In production leave it ``None`` and the
    function reads ``psutil.virtual_memory().available``.
    """
    if sum_container_limits_mb < 0:
        raise ValueError(f"sum_container_limits_mb must be >= 0, got {sum_container_limits_mb}")
    if multiplier < 1.0:
        raise ValueError(f"multiplier must be >= 1.0, got {multiplier}")
    needed = int(sum_container_limits_mb * multiplier)
    have = available_mb if available_mb is not None else _available_mb()
    if have < needed:
        raise MemoryGuardError(
            f"insufficient host RAM before deploy: need {needed} MB "
            f"({multiplier}x {sum_container_limits_mb} MB cap), have {have} MB"
        )
    log.info(
        "headroom ok: have=%dMB needed=%dMB (%.1fx of %dMB cap)",
        have,
        needed,
        multiplier,
        sum_container_limits_mb,
    )


def assert_recovered_to_baseline(
    baseline_available_mb: int,
    slack_mb: int = BASELINE_SLACK_MB,
    timeout_s: int = RECOVERY_TIMEOUT_S,
    *,
    sampler: Callable[[], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Poll host available memory every second until recovered or timeout.

    Returns the final ``host_available_mb`` reading on success.
    Raises :class:`MemoryGuardError` on timeout with the last seen reading.

    ``sampler`` / ``sleeper`` are test seams; production leaves both default.
    """
    if baseline_available_mb < 0:
        raise ValueError(f"baseline_available_mb must be >= 0, got {baseline_available_mb}")
    if slack_mb < 0:
        raise ValueError(f"slack_mb must be >= 0, got {slack_mb}")
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be > 0, got {timeout_s}")
    read = sampler or _available_mb
    threshold = baseline_available_mb - slack_mb
    deadline = time.monotonic() + timeout_s
    while True:
        last = read()
        if last >= threshold:
            log.info(
                "memory recovered: available=%dMB threshold=%dMB (baseline %dMB - slack %dMB)",
                last,
                threshold,
                baseline_available_mb,
                slack_mb,
            )
            return last
        if time.monotonic() >= deadline:
            raise MemoryGuardError(
                f"host memory did not recover within {timeout_s}s: "
                f"available={last} MB, baseline={baseline_available_mb} MB, "
                f"slack={slack_mb} MB, threshold={threshold} MB"
            )
        sleeper(1.0)
