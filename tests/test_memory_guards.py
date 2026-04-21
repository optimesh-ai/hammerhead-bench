"""Memory guard tests — hermetic, no real RLIMIT mutation, no real sleeps."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from harness.memory import (
    BASELINE_SLACK_MB,
    PHASE_POST_DEPLOY,
    PHASE_PRE_DEPLOY,
    MemoryGuardError,
    MemorySample,
    append_memory_sample,
    assert_recovered_to_baseline,
    check_headroom_before_deploy,
    guard_preflight_rlimit,
    sample_memory,
)

# ----- headroom ------------------------------------------------------------


def test_headroom_passes_when_available_above_threshold() -> None:
    # 2x 500 = 1000; having 1500 is fine.
    check_headroom_before_deploy(500, multiplier=2.0, available_mb=1500)


def test_headroom_raises_when_available_below_threshold() -> None:
    with pytest.raises(MemoryGuardError) as excinfo:
        check_headroom_before_deploy(1024, multiplier=2.0, available_mb=500)
    msg = str(excinfo.value)
    assert "insufficient host RAM" in msg
    assert "2048 MB" in msg  # 1024 * 2.0
    assert "500 MB" in msg


def test_headroom_exact_threshold_is_accepted() -> None:
    # Exactly at the threshold (needed = have) must not raise.
    check_headroom_before_deploy(500, multiplier=2.0, available_mb=1000)


def test_headroom_rejects_negative_caps() -> None:
    with pytest.raises(ValueError, match="sum_container_limits_mb"):
        check_headroom_before_deploy(-1, available_mb=10_000)


def test_headroom_rejects_multiplier_below_one() -> None:
    with pytest.raises(ValueError, match="multiplier"):
        check_headroom_before_deploy(100, multiplier=0.5, available_mb=10_000)


# ----- baseline recovery ---------------------------------------------------


class _FakeSampler:
    """Deterministic available-memory sampler."""

    def __init__(self, values: list[int]):
        self._values = list(values)
        self.reads = 0

    def __call__(self) -> int:
        if not self._values:
            # Return the last value forever once the scripted sequence runs out.
            return self._last
        self._last = self._values.pop(0)
        self.reads += 1
        return self._last


def test_baseline_recovery_returns_once_memory_returns() -> None:
    sampler = _FakeSampler([3000, 5000, 8000])  # 8000 >= 8000 - 500 slack
    sleeps: list[float] = []
    got = assert_recovered_to_baseline(
        8000,
        slack_mb=500,
        timeout_s=5,
        sampler=sampler,
        sleeper=sleeps.append,
    )
    assert got == 8000
    # We only sleep between failing samples; never after the success.
    assert len(sleeps) == 2


def test_baseline_recovery_accepts_within_slack() -> None:
    # 7501 >= 8000 - 500 = 7500
    sampler = _FakeSampler([7501])
    sleeps: list[float] = []
    got = assert_recovered_to_baseline(
        8000,
        slack_mb=500,
        timeout_s=5,
        sampler=sampler,
        sleeper=sleeps.append,
    )
    assert got == 7501
    assert sleeps == []


def test_baseline_recovery_raises_on_timeout() -> None:
    # Always below threshold; monotonic clock advances because sleeper is real-ish.
    sampler = _FakeSampler([100, 100, 100])
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        # Don't actually sleep; just bump a counter so we can verify pacing.
        calls["n"] += 1

    with pytest.raises(MemoryGuardError) as excinfo:
        # timeout_s=1 and the monotonic deadline check will trip after ~1s of
        # wall-clock; supply a real sleeper proxy that doesn't sleep but forces
        # the loop to exit via real time.
        import time as _time  # noqa: PLC0415
        real_sleep = _time.sleep

        def hybrid_sleeper(s: float) -> None:  # noqa: ARG001 — signature shape
            real_sleep(0.4)
            fake_sleep(s)

        assert_recovered_to_baseline(
            8000,
            slack_mb=500,
            timeout_s=1,
            sampler=sampler,
            sleeper=hybrid_sleeper,
        )
    assert "did not recover" in str(excinfo.value)
    assert "available=100" in str(excinfo.value)


def test_baseline_recovery_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="baseline_available_mb"):
        assert_recovered_to_baseline(-1)
    with pytest.raises(ValueError, match="slack_mb"):
        assert_recovered_to_baseline(1000, slack_mb=-1)
    with pytest.raises(ValueError, match="timeout_s"):
        assert_recovered_to_baseline(1000, timeout_s=0)


# ----- rlimit --------------------------------------------------------------


def test_rlimit_noop_on_darwin_and_windows() -> None:
    if platform.system() == "Linux":
        pytest.skip("rlimit is exercised by a separate Linux-only test")
    # On mac/Windows this must be a silent no-op (just a log warning).
    guard_preflight_rlimit()


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-only")
def test_rlimit_applied_on_linux_and_idempotent() -> None:
    import resource  # noqa: PLC0415 — Linux-only stdlib

    # Apply twice; second call must not raise (idempotent).
    guard_preflight_rlimit()
    soft_a, hard_a = resource.getrlimit(resource.RLIMIT_AS)
    guard_preflight_rlimit()
    soft_b, hard_b = resource.getrlimit(resource.RLIMIT_AS)
    assert (soft_a, hard_a) == (soft_b, hard_b)
    assert soft_a <= 8 * 1024**3


# ----- sampling + jsonl ----------------------------------------------------


def test_sample_memory_populates_phase_and_values() -> None:
    s = sample_memory(
        topology="bgp-ibgp-2node",
        phase=PHASE_PRE_DEPLOY,
        sum_container_limits_mb=512,
    )
    assert s.topology == "bgp-ibgp-2node"
    assert s.phase == PHASE_PRE_DEPLOY
    assert s.host_available_mb > 0
    assert s.rss_harness_mb > 0
    assert s.sum_container_limits_mb == 512
    assert s.timestamp_iso  # non-empty ISO string


def test_sample_memory_rejects_invalid_phase() -> None:
    with pytest.raises(ValueError, match="MemorySample.phase"):
        MemorySample(
            topology="x",
            phase="not-a-phase",
            host_available_mb=100,
            rss_harness_mb=50,
            sum_container_limits_mb=100,
            timestamp_iso="2026-04-21T00:00:00+00:00",
        )


def test_append_memory_sample_writes_jsonlines(tmp_path: Path) -> None:
    mem = tmp_path / "results" / "memory.jsonl"
    s1 = sample_memory(topology="t", phase=PHASE_PRE_DEPLOY, sum_container_limits_mb=256)
    s2 = sample_memory(topology="t", phase=PHASE_POST_DEPLOY, sum_container_limits_mb=256)
    append_memory_sample(mem, s1)
    append_memory_sample(mem, s2)
    lines = mem.read_text().splitlines()
    assert len(lines) == 2
    row1 = json.loads(lines[0])
    row2 = json.loads(lines[1])
    assert row1["phase"] == PHASE_PRE_DEPLOY
    assert row2["phase"] == PHASE_POST_DEPLOY
    # Schema keys are stable and sorted.
    assert list(row1.keys()) == sorted(row1.keys())


def test_baseline_slack_constant_is_nonzero() -> None:
    # Sanity: if someone drops this to 0 by accident, recovery becomes ultra-strict
    # and flakey on any machine with page-cache churn.
    assert BASELINE_SLACK_MB >= 200
