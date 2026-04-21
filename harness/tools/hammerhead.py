"""Hammerhead CLI wrapper — Phase 6 deliverable.

Shells out to ``$HAMMERHEAD_CLI simulate <dir> --format json``, reads the
``SimulateView`` JSON, routes it through ``hammerhead_transform`` to produce
canonical ``NodeFib`` records. The transform lives in its own module so it
has a clear test surface.

Spawn a monitor thread that polls the child process RSS every 100 ms via
psutil for the speed-measurement path.
"""

from __future__ import annotations

from pathlib import Path


def run_hammerhead(_configs_dir: Path, _out_dir: Path) -> dict:  # pragma: no cover - phase 6
    raise NotImplementedError("tools.hammerhead.run_hammerhead: phase 6")
