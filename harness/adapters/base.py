"""Vendor adapter interface.

Each vendor image maps to one adapter implementing this Protocol. The
benchmark pipeline is agnostic to which vendor is under test — it only calls
methods on ``VendorAdapter``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from harness.extract.fib import NodeFib


@runtime_checkable
class VendorAdapter(Protocol):
    """Minimum interface for a vendor ground-truth adapter."""

    kind: str
    """Short lowercase vendor name, e.g. ``"frr"`` or ``"ceos"``."""

    memory_mb: int
    """Per-container memory cap in MiB. The pipeline sums these to decide headroom."""

    def render_clab_node(self, name: str, config_path: Path) -> dict:
        """Return the dict that goes under ``topology.nodes[<name>]`` in the clab YAML.

        Must include a ``memory`` field matching ``self.memory_mb`` so the
        memory guards are consistent with what containerlab enforces.
        """

    def wait_for_convergence(self, node: str, timeout_s: int) -> bool:
        """Poll the vendor device until protocols have converged or timeout.

        Convergence definition per spec:
        - All configured BGP sessions in Established.
        - Route counts stable across two consecutive 15 s samples.

        Returns True on success, False on timeout. Must not raise on transient
        SSH / vtysh failures — those count as "not yet converged".
        """

    def extract_fib(self, node: str) -> NodeFib:
        """Pull the full FIB from ``node`` and return it in canonical form.

        Must read directly from the running container (e.g. ``vtysh -c 'show
        ip route json'``). Must NOT shell out to Batfish or Hammerhead.
        """


class AdapterNotImplementedError(NotImplementedError):
    """Raised by stub adapters. Points callers at the TODO file for that vendor."""

    def __init__(self, vendor: str, todo_path: str):
        super().__init__(
            f"{vendor}: not implemented in v1. See {todo_path} for scope + plan."
        )
