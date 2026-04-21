"""Containerlab ``bridge``-kind adapter — zero-cost L2 plumbing node.

A clab ``bridge`` node is a Linux bridge on the host. It's not a routing
device — no image, no memory cap, no config — but it's useful for topologies
that need a real broadcast segment (ospf-broadcast-4node) rather than a mesh
of P2Ps. The pipeline skips bridge nodes during convergence + FIB extraction.

Design notes:

- ``memory_mb = 0`` so the memory headroom math naturally ignores bridges.
- ``render_clab_node`` returns ``{"kind": "bridge"}`` — no image, no binds,
  no daemons file. The shared topology template special-cases this.
- ``wait_for_convergence`` / ``extract_fib`` raise ``NotImplementedError``;
  the pipeline should never call them for bridges.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.adapters.base import VendorAdapter


@dataclass(frozen=True, slots=True)
class BridgeAdapter(VendorAdapter):
    """Containerlab ``bridge``-kind adapter. L2 plumbing only."""

    kind: str = "bridge"
    memory_mb: int = 0

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        """Clab YAML dict for a bridge node: just the kind field."""
        _ = name, config_path  # bridges don't consume either
        return {"kind": "bridge"}

    def wait_for_convergence(self, node: str, timeout_s: int = 0) -> bool:  # pragma: no cover
        raise NotImplementedError("bridge adapter: convergence is N/A for bridges")

    def extract_fib(self, node: str):  # pragma: no cover
        raise NotImplementedError("bridge adapter: FIB extraction is N/A for bridges")
