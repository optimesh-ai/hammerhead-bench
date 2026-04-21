"""Integration-flavoured tests for :mod:`harness.tools.hammerhead`.

The orchestrator (``run_hammerhead``) is exercised via a :class:`_FakeRunner`
so no real ``hammerhead`` binary is needed. Focus: output file layout,
per-device file naming, stats JSON shape, and error handling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from harness.extract.fib import NodeFib
from harness.tools.hammerhead import (
    HammerheadConfig,
    HammerheadStats,
    SubprocessHammerheadRunner,
    resolve_hammerhead_cli,
    run_hammerhead,
)

# --- fakes ----------------------------------------------------------------


@dataclass
class _FakeRunner:
    """Canned-response runner. Records calls for assertions."""

    simulate_view: dict[str, Any]
    rib_views: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    raise_on_simulate: Exception | None = None
    raise_on_rib: dict[str, Exception] = field(default_factory=dict)

    def simulate(self, cfg: HammerheadConfig, configs_dir: Path) -> dict[str, Any]:
        self.calls.append(("simulate", str(configs_dir)))
        if self.raise_on_simulate is not None:
            raise self.raise_on_simulate
        return self.simulate_view

    def rib(
        self,
        cfg: HammerheadConfig,
        configs_dir: Path,
        device: str,
    ) -> dict[str, Any]:
        self.calls.append(("rib", device))
        if device in self.raise_on_rib:
            raise self.raise_on_rib[device]
        return self.rib_views.get(
            device,
            {"hostname": device, "entries": []},
        )


def _sim_view(hostnames: list[str]) -> dict[str, Any]:
    return {
        "device_count": len(hostnames),
        "devices": [{"hostname": h} for h in hostnames],
    }


def _rib_view(hostname: str, entries: list[dict]) -> dict[str, Any]:
    return {"hostname": hostname, "entries": entries}


# --- happy path -----------------------------------------------------------


def test_run_hammerhead_writes_per_device_files(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"

    runner = _FakeRunner(
        simulate_view=_sim_view(["r1", "r2"]),
        rib_views={
            "r1": _rib_view(
                "r1",
                [
                    {
                        "prefix": "10.0.0.0/24",
                        "protocol": "C",
                        "next_hop_interface": "eth0",
                        "next_hop_ip": "10.0.0.1",
                    },
                ],
            ),
            "r2": _rib_view(
                "r2",
                [
                    {
                        "prefix": "10.0.0.0/24",
                        "protocol": "O",
                        "next_hop_interface": "eth0",
                        "next_hop_ip": "10.0.0.1",
                    },
                ],
            ),
        },
    )

    stats = run_hammerhead(configs, out, topology="triangle", runner=runner)

    assert (out / "r1__default.json").is_file()
    assert (out / "r2__default.json").is_file()
    fib_r1 = NodeFib.model_validate_json((out / "r1__default.json").read_text())
    assert fib_r1.node == "r1"
    assert fib_r1.source == "hammerhead"
    assert fib_r1.routes[0].protocol == "connected"

    assert stats.topology == "triangle"
    assert stats.device_count == 2
    assert stats.total_routes == 2
    # Stats file lands alongside the per-device JSON.
    stats_path = out / "hammerhead_stats.json"
    assert stats_path.is_file()
    data = json.loads(stats_path.read_text())
    assert data["device_count"] == 2
    assert data["total_routes"] == 2
    assert "simulate_s" in data and "rib_total_s" in data


def test_run_hammerhead_orders_device_queries_deterministically(tmp_path: Path) -> None:
    runner = _FakeRunner(simulate_view=_sim_view(["zeta", "alpha", "mu"]))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
    )
    rib_calls = [c[1] for c in runner.calls if c[0] == "rib"]
    assert rib_calls == ["alpha", "mu", "zeta"]


def test_run_hammerhead_empty_device_list_writes_only_stats(tmp_path: Path) -> None:
    runner = _FakeRunner(simulate_view=_sim_view([]))
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="empty", runner=runner
    )
    assert stats.device_count == 0
    assert stats.total_routes == 0
    assert (tmp_path / "out" / "hammerhead_stats.json").is_file()
    # No __default.json files.
    json_files = list((tmp_path / "out").glob("*__default.json"))
    assert json_files == []


def test_run_hammerhead_dedupes_duplicate_hostnames(tmp_path: Path) -> None:
    runner = _FakeRunner(simulate_view=_sim_view(["r1", "r1", "r2"]))
    run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    rib_calls = [c[1] for c in runner.calls if c[0] == "rib"]
    # r1 appears once despite the duplicate in simulate output.
    assert rib_calls == ["r1", "r2"]


# --- error handling -------------------------------------------------------


def test_simulate_failure_propagates(tmp_path: Path) -> None:
    runner = _FakeRunner(
        simulate_view={},
        raise_on_simulate=RuntimeError("simulated hammerhead parse error"),
    )
    with pytest.raises(RuntimeError, match="simulated hammerhead parse error"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )


def test_rib_failure_on_one_device_propagates(tmp_path: Path) -> None:
    runner = _FakeRunner(
        simulate_view=_sim_view(["r1", "r2"]),
        rib_views={"r1": _rib_view("r1", [])},
        raise_on_rib={"r2": RuntimeError("rib for r2 exploded")},
    )
    with pytest.raises(RuntimeError, match="rib for r2 exploded"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )
    # r1 got written before r2 raised — that's fine, the harness relies
    # on a clean ``out_dir`` per run to avoid stale artefacts.
    assert (tmp_path / "out" / "r1__default.json").is_file()


# --- subprocess runner ----------------------------------------------------


def test_subprocess_runner_passes_correct_argv(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        seen.append(cmd)
        if cmd[1] == "simulate":
            return 0, json.dumps(_sim_view(["r1"])), ""
        if cmd[1] == "rib":
            return 0, json.dumps(_rib_view("r1", [])), ""
        return 2, "", "unexpected subcommand"

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    cfg = HammerheadConfig(hammerhead_cli="/usr/local/bin/hammerhead", timeout_s=5.0)
    run_hammerhead(
        tmp_path / "configs",
        tmp_path / "out",
        topology="t",
        runner=runner,
        config=cfg,
    )
    assert seen[0][0] == "/usr/local/bin/hammerhead"
    assert seen[0][1] == "simulate"
    assert "--format" in seen[0] and "json" in seen[0]
    assert seen[1][1] == "rib"
    assert "--device" in seen[1]
    idx = seen[1].index("--device")
    assert seen[1][idx + 1] == "r1"


def test_subprocess_runner_nonzero_rc_raises(tmp_path: Path) -> None:
    def fake_run(_cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        return 1, "", "boom"

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    with pytest.raises(RuntimeError, match="hammerhead simulate failed"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )


def test_subprocess_runner_invalid_json_raises(tmp_path: Path) -> None:
    def fake_run(_cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        return 0, "<not json>", ""

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )


def test_subprocess_runner_non_object_json_raises(tmp_path: Path) -> None:
    def fake_run(_cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        return 0, "[1,2,3]", ""

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    with pytest.raises(RuntimeError, match="expected JSON object"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )


def test_missing_binary_raises_when_runner_not_injected(tmp_path: Path) -> None:
    cfg = HammerheadConfig(hammerhead_cli="__definitely_not_on_path__")
    with pytest.raises(RuntimeError, match="hammerhead CLI not found on PATH"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", config=cfg
        )


# --- env resolution -------------------------------------------------------


def test_resolve_hammerhead_cli_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMMERHEAD_CLI", "/env/hammerhead")
    assert resolve_hammerhead_cli(override="/override/hammerhead") == "/override/hammerhead"


def test_resolve_hammerhead_cli_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMMERHEAD_CLI", "/env/hammerhead")
    assert resolve_hammerhead_cli() == "/env/hammerhead"


def test_resolve_hammerhead_cli_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HAMMERHEAD_CLI", raising=False)
    assert resolve_hammerhead_cli() == "hammerhead"


# --- stats shape lock -----------------------------------------------------


def test_stats_json_round_trip(tmp_path: Path) -> None:
    runner = _FakeRunner(simulate_view=_sim_view(["r1"]))
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    data = json.loads((tmp_path / "out" / "hammerhead_stats.json").read_text())
    # Every field on HammerheadStats is serialized.
    for field_name in (
        "topology",
        "started_iso",
        "simulate_s",
        "rib_total_s",
        "device_count",
        "total_routes",
        "total_s",
    ):
        assert field_name in data
    assert data["topology"] == stats.topology


def test_stats_type_is_hammerhead_stats(tmp_path: Path) -> None:
    runner = _FakeRunner(simulate_view=_sim_view([]))
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    assert isinstance(stats, HammerheadStats)
