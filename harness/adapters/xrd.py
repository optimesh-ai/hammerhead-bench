"""Cisco XRd adapter — STUBBED + gated-off by default.

XRd's 4 GiB memory cap means a single 3-node XRd topology eats 12 GiB before
any other process runs. Even with 32 GiB host RAM this leaves no headroom for
Batfish (4 GiB) + harness + macOS overhead. Topologies that require XRd are
skipped unless the user passes ``--allow-xrd`` explicitly (wired in the CLI).

TODO file: docs/TODO_XRD.md.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.base import AdapterNotImplementedError, VendorAdapter


class XrdAdapter(VendorAdapter):
    kind = "xrd"
    memory_mb = 4096

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        raise AdapterNotImplementedError("xrd", "docs/TODO_XRD.md")

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:
        raise AdapterNotImplementedError("xrd", "docs/TODO_XRD.md")

    def extract_fib(self, node: str):
        raise AdapterNotImplementedError("xrd", "docs/TODO_XRD.md")
