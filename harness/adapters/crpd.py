"""Juniper crpd adapter — STUBBED.

Not shipped in v1. Blocker: crpd requires a Juniper SRA image download that
gates on a customer account we don't currently have. The container works
fine under containerlab once imported; the adapter itself is straightforward
(NETCONF RPC + RIB dump via ``show route exact-match``).

TODO file: docs/TODO_CRPD.md (track scope + timeline when added).
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.base import AdapterNotImplementedError, VendorAdapter


class CrpdAdapter(VendorAdapter):
    kind = "crpd"
    memory_mb = 1536

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        raise AdapterNotImplementedError("crpd", "docs/TODO_CRPD.md")

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:
        raise AdapterNotImplementedError("crpd", "docs/TODO_CRPD.md")

    def extract_fib(self, node: str):
        raise AdapterNotImplementedError("crpd", "docs/TODO_CRPD.md")
