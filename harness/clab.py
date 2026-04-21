"""Thin wrapper around the ``containerlab`` CLI.

We only need three operations: deploy, destroy, inspect. Wrapping them here
keeps the pipeline testable (can swap in a ``FakeClab`` in tests) and lets us
put teardown verification in one place.

All operations shell out and surface stderr verbatim on failure. We never
retry — a failed deploy is a signal to abort the topology, not to paper over.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEPLOY_TIMEOUT_S = 600
DESTROY_TIMEOUT_S = 300
INSPECT_TIMEOUT_S = 30


class ClabError(RuntimeError):
    """Raised on any containerlab CLI failure. Always carries stderr/stdout context."""


@dataclass(frozen=True, slots=True)
class DeployedLab:
    """Result of a successful ``containerlab deploy``."""

    topology_yaml: Path
    lab_name: str
    """clab's ``name:`` field. Container names are ``clab-<lab_name>-<node>``."""

    def container_name(self, node: str) -> str:
        """Return the Docker container name for a logical node."""
        return f"clab-{self.lab_name}-{node}"


class ClabDriver(Protocol):
    """Swappable clab interface so pipeline tests don't need Docker."""

    def deploy(self, topology_yaml: Path) -> DeployedLab: ...
    def destroy(self, topology_yaml: Path) -> None: ...
    def dangling_resources(self) -> list[str]: ...


class RealClab:
    """Production ClabDriver that shells out to ``containerlab``."""

    def __init__(self, binary: str | None = None) -> None:
        self._bin = binary or shutil.which("containerlab") or shutil.which("clab")
        if self._bin is None:
            raise ClabError("containerlab CLI not on PATH; run `make preflight`")

    def deploy(self, topology_yaml: Path) -> DeployedLab:
        proc = subprocess.run(
            [self._bin, "deploy", "-t", str(topology_yaml), "--reconfigure"],
            capture_output=True,
            text=True,
            timeout=DEPLOY_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            raise ClabError(f"clab deploy failed: {proc.stderr.strip() or proc.stdout.strip()}")
        lab_name = _lab_name_from_yaml(topology_yaml)
        return DeployedLab(topology_yaml=topology_yaml, lab_name=lab_name)

    def destroy(self, topology_yaml: Path) -> None:
        proc = subprocess.run(
            [self._bin, "destroy", "-t", str(topology_yaml), "--cleanup"],
            capture_output=True,
            text=True,
            timeout=DESTROY_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            raise ClabError(f"clab destroy failed: {proc.stderr.strip() or proc.stdout.strip()}")

    def dangling_resources(self) -> list[str]:
        """Return container names still carrying a clab-topo label, if any.

        Used by the teardown-verification step. On a clean teardown this must
        be empty; if not, the pipeline aborts loudly.
        """
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=clab-topo",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            raise ClabError(f"docker ps failed: {proc.stderr.strip()}")
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _lab_name_from_yaml(topology_yaml: Path) -> str:
    """Extract the ``name:`` field from a clab YAML without a YAML parser.

    We deliberately do not depend on PyYAML; clab topology YAMLs always put the
    ``name:`` on a top-level line, and this keeps our dep list tight.
    """
    for raw in topology_yaml.read_text().splitlines():
        s = raw.strip()
        if s.startswith("name:"):
            return s[len("name:") :].strip().strip('"').strip("'")
    raise ClabError(f"{topology_yaml}: no top-level 'name:' field")


def parse_deploy_json(output: str) -> dict:
    """Parse ``clab deploy --format json`` output. Robust to trailing banner lines."""
    output = output.strip()
    if not output:
        return {}
    # clab sometimes prints a banner before the JSON blob; find the first '{'.
    idx = output.find("{")
    if idx < 0:
        return {}
    return json.loads(output[idx:])
