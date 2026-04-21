"""Pure transform: Hammerhead ``SimulateView`` JSON -> list[NodeFib].

Kept out of ``hammerhead.py`` so the transform has its own test surface
(``tests/test_hammerhead_transform.py``, phase 6). When Hammerhead's output
schema evolves, this is the single place that needs to change.

Phase 1 ships only the function signature; the real transform lands in phase 6.
"""

from __future__ import annotations

from typing import Any

from harness.extract.fib import NodeFib


def transform_simulate_view(_view: dict[str, Any]) -> list[NodeFib]:  # pragma: no cover - phase 6
    raise NotImplementedError("tools.hammerhead_transform.transform_simulate_view: phase 6")
