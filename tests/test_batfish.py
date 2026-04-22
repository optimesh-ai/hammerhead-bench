"""Batfish wrapper tests — Phase 5. Hermetic: no docker, no real pybatfish."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.extract.fib import NodeFib
from harness.tools.batfish import (
    BATFISH_MEMORY_MB,
    BatfishConfig,
    BatfishRunner,
    BatfishSession,
    run_batfish,
    transform_batfish_rows,
)

# ---- transform ------------------------------------------------------------


def test_transform_single_connected_route_nested_next_hop() -> None:
    rows = [
        {
            "Node": "r1",
            "VRF": "default",
            "Network": "10.0.0.1/32",
            "Protocol": "connected",
            "Next_Hop": {"ip": None, "interface": "lo"},
            "Admin_Distance": 0,
            "Metric": 0,
        }
    ]
    [fib] = transform_batfish_rows(rows)
    assert fib.node == "r1"
    assert fib.vrf == "default"
    assert fib.source == "batfish"
    assert len(fib.routes) == 1
    r = fib.routes[0]
    assert r.prefix == "10.0.0.1/32"
    assert r.protocol == "connected"
    assert r.next_hops[0].interface == "lo"
    assert r.admin_distance == 0


def test_transform_flat_next_hop_fields_work() -> None:
    rows = [
        {
            "Node": "r1",
            "VRF": "default",
            "Network": "10.1.0.0/24",
            "Protocol": "ospf-ia",
            "Next_Hop_IP": "10.0.12.2",
            "Next_Hop_Interface": "Ethernet1",
            "Admin_Distance": 110,
            "Metric": 11,
        }
    ]
    [fib] = transform_batfish_rows(rows)
    r = fib.routes[0]
    assert r.protocol == "ospf"  # ospf-ia normalizes to ospf
    assert r.next_hops[0].ip == "10.0.12.2"
    assert r.next_hops[0].interface == "Ethernet1"


def test_transform_ibgp_and_ebgp_labels_map_to_bgp() -> None:
    rows = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "ibgp", "Next_Hop_IP": "10.0.12.2", "Next_Hop_Interface": None},
        {"Node": "r1", "VRF": "default", "Network": "10.1.0.0/24",
         "Protocol": "ebgp", "Next_Hop_IP": "10.0.13.3", "Next_Hop_Interface": None},
    ]
    [fib] = transform_batfish_rows(rows)
    assert {r.protocol for r in fib.routes} == {"bgp"}
    assert len(fib.routes) == 2


def test_transform_unknown_protocol_raises() -> None:
    rows = [{"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
             "Protocol": "mystery-proto"}]
    with pytest.raises(ValueError, match="unknown Batfish protocol"):
        transform_batfish_rows(rows)


def test_transform_kernel_protocol_is_silently_dropped() -> None:
    rows = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "kernel"},
        {"Node": "r1", "VRF": "default", "Network": "10.1.0.0/24",
         "Protocol": "connected", "Next_Hop_Interface": "eth1"},
    ]
    [fib] = transform_batfish_rows(rows)
    assert [r.prefix for r in fib.routes] == ["10.1.0.0/24"]


def test_transform_vrf_alias_collapses() -> None:
    rows = [
        {"Node": "r1", "VRF": "", "Network": "10.0.0.0/24",
         "Protocol": "connected", "Next_Hop_Interface": "eth1"},
        {"Node": "r1", "VRF": "global", "Network": "10.1.0.0/24",
         "Protocol": "connected", "Next_Hop_Interface": "eth2"},
    ]
    fibs = transform_batfish_rows(rows)
    # Both VRFs collapse to "default" so we get one merged NodeFib.
    assert len(fibs) == 1
    assert fibs[0].vrf == "default"
    assert len(fibs[0].routes) == 2


def test_transform_ecmp_list_next_hops() -> None:
    rows = [
        {
            "Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
            "Protocol": "ospf",
            "Next_Hop_IP": ["10.0.12.2", "10.0.13.3"],
            "Next_Hop_Interface": ["eth1", "eth2"],
        }
    ]
    [fib] = transform_batfish_rows(rows)
    r = fib.routes[0]
    assert len(r.next_hops) == 2
    # Canonicalized: sorted by (ip, iface).
    assert [n.ip for n in r.next_hops] == ["10.0.12.2", "10.0.13.3"]


def test_transform_bgp_attrs_merged_from_bgp_rib_rows() -> None:
    routes = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "bgp", "Next_Hop_IP": "10.0.12.2", "Next_Hop_Interface": None},
    ]
    bgp_rib = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Status": ["BEST", "INSTALLED"],
         "AS_Path": [65001, 65002],
         "Local_Pref": 200,
         "Metric": 50,
         "Communities": ["65001:100", "65001:200"]},
    ]
    [fib] = transform_batfish_rows(routes, bgp_rows=bgp_rib)
    r = fib.routes[0]
    assert r.as_path == [65001, 65002]
    assert r.local_pref == 200
    assert r.med == 50
    assert r.communities == ["65001:100", "65001:200"]


def test_transform_bgp_rib_ignores_non_best_rows() -> None:
    routes = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "bgp", "Next_Hop_IP": "10.0.12.2", "Next_Hop_Interface": None},
    ]
    bgp_rib = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Status": ["BACKUP"],
         "AS_Path": [65099],
         "Local_Pref": 50,
         "Metric": 999},
    ]
    [fib] = transform_batfish_rows(routes, bgp_rows=bgp_rib)
    r = fib.routes[0]
    # BACKUP status => not merged; attrs stay None.
    assert r.as_path is None
    assert r.local_pref is None
    assert r.med is None


def test_transform_bgp_aspath_string_form() -> None:
    routes = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "bgp", "Next_Hop_IP": "10.0.12.2", "Next_Hop_Interface": None},
    ]
    bgp_rib = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Status": ["BEST"], "AS_Path": "65001 65002 65003",
         "Local_Pref": 100},
    ]
    [fib] = transform_batfish_rows(routes, bgp_rows=bgp_rib)
    assert fib.routes[0].as_path == [65001, 65002, 65003]


def test_transform_empty_rows_returns_empty_list() -> None:
    assert transform_batfish_rows([]) == []


def test_transform_missing_node_or_network_silently_drops_row() -> None:
    rows = [
        {"Node": "", "VRF": "default", "Network": "10.0.0.0/24", "Protocol": "connected"},
        {"Node": "r1", "VRF": "default", "Network": "", "Protocol": "connected"},
        {"Node": "r1", "VRF": "default", "Network": "10.1.0.0/24",
         "Protocol": "connected", "Next_Hop_Interface": "eth1"},
    ]
    [fib] = transform_batfish_rows(rows)
    assert [r.prefix for r in fib.routes] == ["10.1.0.0/24"]


def test_transform_dynamic_next_hop_interface_treated_as_none() -> None:
    rows = [
        {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
         "Protocol": "bgp",
         "Next_Hop": {"ip": "10.0.12.2", "interface": "dynamic"}},
    ]
    [fib] = transform_batfish_rows(rows)
    assert fib.routes[0].next_hops[0].interface is None


# ---- orchestration (hermetic) --------------------------------------------


class _FakeSession:
    def __init__(self, routes: list[dict[str, Any]], bgp: list[dict[str, Any]]) -> None:
        self._routes = routes
        self._bgp = bgp
        self.inits: list[tuple[str, str]] = []

    def init_snapshot(self, path: str, name: str, overwrite: bool = True) -> str:
        self.inits.append((path, name))
        return name

    def get_routes(self) -> list[dict[str, Any]]:
        return list(self._routes)

    def get_bgp_rib(self) -> list[dict[str, Any]]:
        return list(self._bgp)


class _FakeRunner:
    def __init__(self, fail_wait: bool = False) -> None:
        self.started: list[BatfishConfig] = []
        self.stopped: list[str] = []
        self.fail_wait = fail_wait
        self.wait_calls = 0

    def start(self, cfg: BatfishConfig) -> str:
        self.started.append(cfg)
        return "fake-container-id"

    def wait_ready(self, cfg: BatfishConfig, container_id: str) -> None:
        self.wait_calls += 1
        assert container_id  # non-empty — the harness passes start()'s return here
        if self.fail_wait:
            raise TimeoutError(f"Batfish did not start within {cfg.startup_timeout_s}s")

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


def test_run_batfish_writes_per_node_vrf_json(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"

    session = _FakeSession(
        routes=[
            {"Node": "r1", "VRF": "default", "Network": "10.0.0.1/32",
             "Protocol": "connected", "Next_Hop": {"interface": "lo"}},
            {"Node": "r2", "VRF": "default", "Network": "10.0.0.2/32",
             "Protocol": "connected", "Next_Hop": {"interface": "lo"}},
        ],
        bgp=[],
    )
    runner = _FakeRunner()

    stats = run_batfish(
        configs, out,
        topology="bgp-ibgp-2node",
        session_factory=lambda _cfg: session,
        runner=runner,
    )

    # Both lifecycle calls fired.
    assert runner.started and runner.stopped == ["fake-container-id"]
    assert runner.wait_calls == 1

    # init_snapshot got a staged snapshot root (not configs itself).
    # Batfish requires <root>/configs/<device>.cfg layout; the harness
    # stages a temp root and copies configs in flat.
    staged_root = Path(session.inits[0][0])
    assert staged_root.name.startswith("bf-snap-")
    assert session.inits[0][1] == "bench-bgp-ibgp-2node"

    # One JSON per (node, vrf).
    files = sorted(p.name for p in out.iterdir() if p.suffix == ".json")
    assert files == ["batfish_stats.json", "r1__default.json", "r2__default.json"]

    # FIB round-trips through the schema.
    payload = json.loads((out / "r1__default.json").read_text())
    assert payload["source"] == "batfish"
    assert payload["node"] == "r1"
    assert payload["vrf"] == "default"
    assert payload["routes"][0]["prefix"] == "10.0.0.1/32"

    # Stats look sane.
    assert stats.topology == "bgp-ibgp-2node"
    assert stats.total_s >= 0
    stats_payload = json.loads((out / "batfish_stats.json").read_text())
    assert stats_payload["topology"] == "bgp-ibgp-2node"


def test_run_batfish_stops_container_even_on_session_failure(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"
    runner = _FakeRunner()

    class _Exploding:
        def init_snapshot(self, *_a, **_kw) -> str:
            raise RuntimeError("simulated Batfish snapshot parse error")

        def get_routes(self) -> list[dict[str, Any]]:  # pragma: no cover
            raise AssertionError("should not be called")

        def get_bgp_rib(self) -> list[dict[str, Any]]:  # pragma: no cover
            raise AssertionError("should not be called")

    with pytest.raises(RuntimeError, match="simulated Batfish"):
        run_batfish(
            configs, out,
            topology="bgp-ibgp-2node",
            session_factory=lambda _cfg: _Exploding(),  # type: ignore[arg-type]
            runner=runner,
        )
    # Container was still stopped.
    assert runner.stopped == ["fake-container-id"]


def test_run_batfish_wait_ready_failure_still_stops_container(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"
    runner = _FakeRunner(fail_wait=True)

    with pytest.raises(TimeoutError):
        run_batfish(
            configs, out,
            topology="bgp-ibgp-2node",
            session_factory=lambda _cfg: _FakeSession([], []),
            runner=runner,
        )
    assert runner.stopped == ["fake-container-id"]


def test_run_batfish_protocol_classes_cover_abstract_surface() -> None:
    # Defensive: ensure the two Protocol surfaces are reachable at runtime so
    # a future refactor doesn't accidentally drop the test seams.
    assert BatfishSession is not None
    assert BatfishRunner is not None
    assert BATFISH_MEMORY_MB == 4096
    cfg = BatfishConfig()
    assert cfg.memory_mb == BATFISH_MEMORY_MB


def test_run_batfish_writes_bgp_attrs_from_rib(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"

    session = _FakeSession(
        routes=[
            {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
             "Protocol": "bgp", "Next_Hop_IP": "10.0.12.2", "Next_Hop_Interface": None},
        ],
        bgp=[
            {"Node": "r1", "VRF": "default", "Network": "10.0.0.0/24",
             "Status": ["BEST"], "AS_Path": [65001, 65002],
             "Local_Pref": 100, "Metric": 0},
        ],
    )
    run_batfish(
        configs, out,
        topology="bgp-ibgp-2node",
        session_factory=lambda _cfg: session,
        runner=_FakeRunner(),
    )
    fib = NodeFib.model_validate_json((out / "r1__default.json").read_text())
    r = fib.routes[0]
    assert r.as_path == [65001, 65002]
    assert r.local_pref == 100
    assert r.med == 0
