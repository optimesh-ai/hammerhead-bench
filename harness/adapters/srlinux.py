"""Nokia SR Linux adapter — STUBBED.

Not shipped in v1. Adapter will use the srlinux gNMI surface for convergence
+ FIB extraction (``network-instance/default/route-table/ipv4-unicast/route``).

TODO file: docs/TODO_SRLINUX.md.
"""

from __future__ import annotations

from pathlib import Path

from harness.adapters.base import AdapterNotImplementedError, VendorAdapter


class SrLinuxAdapter(VendorAdapter):
    kind = "srlinux"
    memory_mb = 2048

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        raise AdapterNotImplementedError("srlinux", "docs/TODO_SRLINUX.md")

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:
        raise AdapterNotImplementedError("srlinux", "docs/TODO_SRLINUX.md")

    def extract_fib(self, node: str):
        raise AdapterNotImplementedError("srlinux", "docs/TODO_SRLINUX.md")
