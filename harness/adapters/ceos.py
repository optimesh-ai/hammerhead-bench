"""Arista cEOS-lab adapter — FULL implementation lands in phase 8.

Image is user-supplied (Arista EOS Central, free account). Expected tag
documented in ``versions.lock``.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.base import VendorAdapter


class CeosAdapter(VendorAdapter):
    kind = "ceos"
    memory_mb = 1024

    def render_clab_node(self, name: str, config_path: Path) -> dict:  # pragma: no cover - phase 8
        raise NotImplementedError("ceos.render_clab_node: phase 8")

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:  # pragma: no cover - phase 8
        raise NotImplementedError("ceos.wait_for_convergence: phase 8")

    def extract_fib(self, node: str):  # pragma: no cover - phase 8
        raise NotImplementedError("ceos.extract_fib: phase 8")
