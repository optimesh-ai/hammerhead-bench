"""pybatfish wrapper — Phase 5 deliverable.

Runs Batfish in a dockerized container with ``_JAVA_OPTIONS=-Xmx4g`` applied,
uploads the topology's config dir, extracts per-node routes, writes canonical
``NodeFib`` JSON to disk, destroys the container. Memory-disciplined.
"""

from __future__ import annotations

from pathlib import Path


def run_batfish(_configs_dir: Path, _out_dir: Path) -> dict:  # pragma: no cover - phase 5
    raise NotImplementedError("tools.batfish.run_batfish: phase 5")
