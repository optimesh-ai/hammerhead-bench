"""FRRouting adapter — FULL implementation lands in phase 2.

Phase 1: declare the per-vendor memory cap and node-kind so preflight and
pipeline scaffolding compiles without the convergence/extraction logic.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.base import VendorAdapter


class FrrAdapter(VendorAdapter):
    kind = "frr"
    memory_mb = 256

    def render_clab_node(self, name: str, config_path: Path) -> dict:  # pragma: no cover - phase 2
        raise NotImplementedError("frr.render_clab_node: phase 2")

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:  # pragma: no cover - phase 2
        raise NotImplementedError("frr.wait_for_convergence: phase 2")

    def extract_fib(self, node: str):  # pragma: no cover - phase 2
        raise NotImplementedError("frr.extract_fib: phase 2")
