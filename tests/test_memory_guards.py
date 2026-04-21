"""Memory guard tests — Phase 3 deliverable.

Phase 1 ships marker tests that the stub entry points raise
NotImplementedError. The real test matrix (trigger headroom failure,
assert recovery to baseline, RLIMIT_AS applied on Linux) lands in phase 3.
"""

from __future__ import annotations

import pytest

from harness.memory import (
    assert_recovered_to_baseline,
    check_headroom_before_deploy,
    guard_preflight_rlimit,
)


def test_guard_preflight_rlimit_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        guard_preflight_rlimit()


def test_check_headroom_before_deploy_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        check_headroom_before_deploy(1024)


def test_assert_recovered_to_baseline_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        assert_recovered_to_baseline(8192)
