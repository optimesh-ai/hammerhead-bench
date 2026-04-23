"""Unit tests for :mod:`harness.peak_rss`.

Covers the three public surfaces:

1. :func:`rusage_peak_mb` — runs a callable, returns its result + a
   :class:`PeakRssReading`. Platform-dependent divisor is normalised.
2. :class:`DockerStatsSampler` — injects a fake runner, asserts that
   the sampler records the max over the poll window, that it handles
   poll failures gracefully, and that ``stop()`` reports zero samples
   when no poll succeeded.
3. :func:`peak_rss_enabled` — env-var guard flips correctly.

None of these tests shell out. The docker runner is injected; rusage is
exercised with an identity callable.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from harness.peak_rss import (
    PEAK_RSS_ENV_DISABLE,
    DockerStatsSampler,
    PeakRssReading,
    peak_rss_enabled,
    rusage_peak_mb,
)


# ---- rusage --------------------------------------------------------------


def test_rusage_peak_mb_runs_body_and_returns_reading():
    result, reading = rusage_peak_mb(lambda: 42)
    assert result == 42
    # On CI the child rusage bucket may already carry a positive value
    # from pytest's own subprocess invocations; we don't assert a
    # specific MB figure, only that the reading is well-typed.
    assert isinstance(reading, PeakRssReading)
    assert reading.source == "rusage"
    # Either we got a reading or we didn't — the shape is the same.
    if reading.mb is not None:
        assert reading.mb > 0
        assert reading.sample_count == 1
    else:
        assert reading.sample_count == 0


def test_rusage_peak_mb_propagates_body_exception():
    with pytest.raises(ValueError, match="boom"):
        rusage_peak_mb(lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_rusage_peak_mb_source_label_is_carried_through():
    _, reading = rusage_peak_mb(lambda: None, source="custom-label")
    assert reading.source == "custom-label"


# ---- DockerStatsSampler --------------------------------------------------


class _FakeRunner:
    """Injectable docker-stats runner returning canned outputs.

    Accepts a list of ``(rc, stdout, stderr)`` tuples; each call pops
    the next one. A subsequent call after exhaustion returns the last
    tuple repeatedly so the sampler keeps polling while the test
    waits.
    """

    def __init__(self, outputs: list[tuple[int, str, str]]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, cmd: list[str], timeout_s: float) -> tuple[int, str, str]:
        with self._lock:
            self.calls += 1
            if not self._outputs:
                return 0, "1MiB / 4GiB\n", ""
            if len(self._outputs) == 1:
                return self._outputs[0]
            return self._outputs.pop(0)


def _wait_until(predicate, timeout_s: float = 2.0) -> bool:
    """Poll ``predicate`` every 20 ms until true or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_docker_stats_sampler_records_max_over_window():
    runner = _FakeRunner(
        [
            (0, "512MiB / 4GiB\n", ""),
            (0, "1.5GiB / 4GiB\n", ""),   # peak
            (0, "768MiB / 4GiB\n", ""),
            (0, "1.2GiB / 4GiB\n", ""),
        ]
    )
    sampler = DockerStatsSampler(
        container_id="fake",
        interval_s=0.05,
        runner=runner,
    )
    sampler.start()
    # Wait until the runner has been called at least 4 times so we know
    # the peak (1.5 GiB sample) landed.
    assert _wait_until(lambda: runner.calls >= 4, timeout_s=2.0)
    reading = sampler.stop(timeout_s=1.0)

    assert reading.source == "docker-stats"
    assert reading.sample_count >= 1
    # 1.5 GiB = 1536 MiB — rounded to int.
    assert reading.mb is not None
    assert reading.mb == 1536


def test_docker_stats_sampler_handles_poll_errors_as_skipped():
    # First two polls fail; third succeeds — sample_count should be 1+.
    runner = _FakeRunner(
        [
            (127, "", "docker: command not found"),
            (1, "", "container not running"),
            (0, "256MiB / 4GiB\n", ""),
        ]
    )
    sampler = DockerStatsSampler(
        container_id="fake",
        interval_s=0.05,
        runner=runner,
    )
    sampler.start()
    assert _wait_until(lambda: runner.calls >= 3, timeout_s=2.0)
    reading = sampler.stop(timeout_s=1.0)

    assert reading.sample_count >= 1
    assert reading.mb == 256


def test_docker_stats_sampler_zero_samples_when_all_polls_fail():
    runner = _FakeRunner([(127, "", "no docker")])
    sampler = DockerStatsSampler(
        container_id="fake",
        interval_s=0.05,
        runner=runner,
    )
    sampler.start()
    # Poll for long enough that at least one sample-attempt fired.
    assert _wait_until(lambda: runner.calls >= 3, timeout_s=2.0)
    reading = sampler.stop(timeout_s=1.0)

    assert reading.source == "docker-stats"
    assert reading.mb is None
    assert reading.sample_count == 0


def test_docker_stats_sampler_double_start_rejected():
    sampler = DockerStatsSampler(
        container_id="fake",
        interval_s=0.05,
        runner=_FakeRunner([]),
    )
    sampler.start()
    with pytest.raises(RuntimeError, match="already started"):
        sampler.start()
    sampler.stop(timeout_s=1.0)


def test_docker_stats_sampler_stop_without_start_returns_empty_reading():
    sampler = DockerStatsSampler(
        container_id="fake",
        runner=_FakeRunner([]),
    )
    reading = sampler.stop()
    assert reading.mb is None
    assert reading.sample_count == 0
    assert reading.source == "docker-stats"


def test_docker_stats_sampler_parses_all_units():
    # Runner that cycles through every unit the regex should accept;
    # the sampler picks the max. Order matters — we want TiB to win.
    runner = _FakeRunner(
        [
            (0, "42B / 4GiB\n", ""),
            (0, "512KiB / 4GiB\n", ""),
            (0, "768MiB / 4GiB\n", ""),
            (0, "2GiB / 4GiB\n", ""),
            (0, "1TiB / 4GiB\n", ""),  # 1 TiB = 1048576 MiB
        ]
    )
    sampler = DockerStatsSampler(
        container_id="fake",
        interval_s=0.05,
        runner=runner,
    )
    sampler.start()
    assert _wait_until(lambda: runner.calls >= 5, timeout_s=3.0)
    reading = sampler.stop(timeout_s=1.0)

    assert reading.mb is not None
    assert reading.mb == 1024 * 1024  # 1 TiB in MiB


# ---- env guard -----------------------------------------------------------


def test_peak_rss_enabled_defaults_to_true(monkeypatch):
    monkeypatch.delenv(PEAK_RSS_ENV_DISABLE, raising=False)
    assert peak_rss_enabled() is True


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE", "Yes"])
def test_peak_rss_enabled_respects_disable_flag(monkeypatch, flag):
    monkeypatch.setenv(PEAK_RSS_ENV_DISABLE, flag)
    assert peak_rss_enabled() is False


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "random"])
def test_peak_rss_enabled_ignores_other_values(monkeypatch, flag):
    monkeypatch.setenv(PEAK_RSS_ENV_DISABLE, flag)
    assert peak_rss_enabled() is True
