"""Memory guards — the #1 correctness constraint of this harness.

Rules (enforced in phase 3; types defined here so other modules can import):

- One topology deployed at a time.
- Before deploying a topology, host available RAM must exceed
  ``MEMORY_HEADROOM_MULTIPLIER * sum(container caps)``; default 2.0.
- On Linux the harness process sets ``RLIMIT_AS = 8 GiB``. On macOS this is
  skipped with a warning (setrlimit behaves erratically on Darwin).
- After each teardown, host memory must return within ``BASELINE_SLACK_MB``
  (default 500 MB) of the pre-topology baseline within ``RECOVERY_TIMEOUT_S``
  (default 30 s). If not, the whole run aborts with a dangling-resource report.
- Memory samples are written to ``results/memory.jsonl`` as one JSON object
  per topology with fields documented in :class:`MemorySample`.

Guards are intentionally load-bearing and crash loudly. Do NOT catch-and-swallow
from this module in calling code — let the ``MemoryGuardError`` propagate.
"""

from __future__ import annotations

from dataclasses import dataclass

RLIMIT_AS_BYTES = 8 * 1024**3  # 8 GiB; Linux only.
BASELINE_SLACK_MB = 500
RECOVERY_TIMEOUT_S = 30
DEFAULT_HEADROOM_MULTIPLIER = 2.0


class MemoryGuardError(RuntimeError):
    """Raised when a memory invariant is violated. Never caught inside harness."""


@dataclass(frozen=True, slots=True)
class MemorySample:
    """One row of ``results/memory.jsonl``; emitted per topology."""

    topology: str
    phase: str  # "pre-deploy" | "post-deploy" | "post-teardown" | "recovered"
    host_available_mb: int
    rss_harness_mb: int
    sum_container_limits_mb: int
    timestamp_iso: str


def _not_implemented(name: str) -> object:  # pragma: no cover - phase 3
    raise NotImplementedError(
        f"{name}: implemented in phase 3 (memory guards + teardown verification)"
    )


def guard_preflight_rlimit() -> None:  # pragma: no cover - phase 3
    """Apply ``RLIMIT_AS`` on Linux, log a warning elsewhere."""
    _not_implemented("guard_preflight_rlimit")


def check_headroom_before_deploy(
    sum_container_limits_mb: int,
    multiplier: float = DEFAULT_HEADROOM_MULTIPLIER,
) -> None:  # pragma: no cover - phase 3
    """Raise ``MemoryGuardError`` if host RAM is below ``multiplier * cap``."""
    _not_implemented("check_headroom_before_deploy")


def assert_recovered_to_baseline(
    baseline_available_mb: int,
    slack_mb: int = BASELINE_SLACK_MB,
    timeout_s: int = RECOVERY_TIMEOUT_S,
) -> None:  # pragma: no cover - phase 3
    """Poll host memory every second; raise if we don't return to baseline."""
    _not_implemented("assert_recovered_to_baseline")
