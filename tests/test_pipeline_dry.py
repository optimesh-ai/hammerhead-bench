"""Pipeline dry-run tests — exercise orchestration without docker.

We swap in a ``FakeClab`` + monkey-patched adapter so the test matrix covers:

- Render → (fake) deploy → (stubbed) converge → (stubbed) extract → destroy
  happy path produces vendor_truth JSON on disk.
- Convergence timeout propagates as a failed TopologyRunResult without
  leaving the lab up (destroy still runs unless --keep-lab-on-failure).
- Dangling container post-destroy appears as a ``note`` on the result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harness.adapters.frr import FrrAdapter
from harness.clab import DeployedLab
from harness.extract.fib import NextHop, NodeFib, Route
from harness.pipeline import run_topology
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"


@dataclass
class FakeClab:
    """In-memory ClabDriver for pipeline tests. Never touches docker."""

    deployed_paths: list[Path] = field(default_factory=list)
    destroyed_paths: list[Path] = field(default_factory=list)
    dangling: list[str] = field(default_factory=list)
    deploy_raises: Exception | None = None

    def deploy(self, topology_yaml: Path) -> DeployedLab:
        if self.deploy_raises is not None:
            raise self.deploy_raises
        self.deployed_paths.append(topology_yaml)
        return DeployedLab(topology_yaml=topology_yaml, lab_name="hh-bench-bgp-ibgp-2node")

    def destroy(self, topology_yaml: Path) -> None:
        self.destroyed_paths.append(topology_yaml)

    def dangling_resources(self) -> list[str]:
        return list(self.dangling)


@pytest.fixture
def patched_adapter(monkeypatch):
    """Stub convergence + extract so the pipeline runs without docker."""

    def fake_wait(self, container, timeout_s=300):  # noqa: ARG001
        return True

    def fake_extract(self, container, node_name=None):  # noqa: ARG001
        name = node_name or container
        return [
            NodeFib(
                node=name,
                vrf="default",
                source="vendor",
                routes=[
                    Route(
                        prefix=f"10.0.0.{'1' if name == 'r1' else '2'}/32",
                        protocol="connected",
                        next_hops=[NextHop(interface="lo")],
                        admin_distance=0,
                        metric=0,
                    ),
                ],
            )
        ]

    monkeypatch.setattr(FrrAdapter, "wait_for_convergence", fake_wait)
    monkeypatch.setattr(FrrAdapter, "extract_fib", fake_extract)


def test_run_topology_happy_path_writes_vendor_truth(tmp_path: Path, patched_adapter) -> None:  # noqa: ARG001
    spec = load_spec(TOPO_DIR)
    clab = FakeClab()
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
    )
    assert result.status == "passed", result.error
    assert result.error is None

    # Deploy + destroy both fired.
    assert len(clab.deployed_paths) == 1
    assert len(clab.destroyed_paths) == 1

    # Each node got a vendor_truth JSON file.
    vt = tmp_path / "results" / "vendor_truth" / "bgp-ibgp-2node"
    assert sorted(p.name for p in vt.iterdir()) == ["r1__default.json", "r2__default.json"]
    payload = json.loads((vt / "r1__default.json").read_text())
    assert payload["node"] == "r1"
    assert payload["vrf"] == "default"
    assert payload["source"] == "vendor"


def test_run_topology_convergence_timeout_marks_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(FrrAdapter, "wait_for_convergence", lambda self, c, timeout_s=300: False)  # noqa: ARG005
    monkeypatch.setattr(FrrAdapter, "extract_fib", lambda self, c, node_name=None: [])  # noqa: ARG005

    spec = load_spec(TOPO_DIR)
    clab = FakeClab()
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
    )
    assert result.status == "failed"
    assert "did not converge" in (result.error or "")
    # Destroy still fired (keep_lab_on_failure defaults to False).
    assert len(clab.destroyed_paths) == 1


def test_run_topology_dangling_container_appears_as_note(tmp_path: Path, patched_adapter) -> None:  # noqa: ARG001
    spec = load_spec(TOPO_DIR)
    clab = FakeClab(dangling=["clab-hh-bench-bgp-ibgp-2node-r1"])
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
    )
    assert result.status == "passed"
    assert any("dangling clab containers" in n for n in result.notes)


def test_run_topology_keep_lab_on_failure_skips_destroy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(FrrAdapter, "wait_for_convergence", lambda self, c, timeout_s=300: False)  # noqa: ARG005
    monkeypatch.setattr(FrrAdapter, "extract_fib", lambda self, c, node_name=None: [])  # noqa: ARG005

    spec = load_spec(TOPO_DIR)
    clab = FakeClab()
    result = run_topology(
        spec,
        workdir=tmp_path / "workdir",
        results_dir=tmp_path / "results",
        clab=clab,
        keep_lab_on_failure=True,
    )
    assert result.status == "failed"
    assert clab.destroyed_paths == []
