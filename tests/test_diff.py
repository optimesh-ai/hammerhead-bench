"""Diff engine tests — Phase 4 deliverable.

Phase 1 ships a marker test that the stub raises NotImplementedError so we
don't accidentally green the CI with a no-op implementation later.
"""

from __future__ import annotations

import pytest

from harness.diff.engine import diff_fibs


def test_diff_engine_stubbed() -> None:
    with pytest.raises(NotImplementedError):
        diff_fibs()
