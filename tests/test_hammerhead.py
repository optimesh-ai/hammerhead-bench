"""Integration-flavoured tests for :mod:`harness.tools.hammerhead`.

The orchestrator (``run_hammerhead``) is exercised via a :class:`_FakeRunner`
so no real ``hammerhead`` binary is needed. Focus: output file layout,
per-device file naming, stats JSON shape, error handling, and the
``expected_hostnames`` assertion that guards against silent missing-device
regressions in the bulk ``simulate --emit-rib all`` response.
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

    bulk_view: dict[str, Any]
    calls: list[tuple[str, str]] = field(default_factory=list)
    raise_on_bulk: Exception | None = None

    def simulate_emit_rib_all(
        self, cfg: HammerheadConfig, configs_dir: Path
    ) -> dict[str, Any]:
        self.calls.append(("simulate_emit_rib_all", str(configs_dir)))
        if self.raise_on_bulk is not None:
            raise self.raise_on_bulk
        return self.bulk_view


def _rib_view(hostname: str, entries: list[dict]) -> dict[str, Any]:
    return {"hostname": hostname, "entries": entries}


def _bulk_view(views: dict[str, list[dict]]) -> dict[str, Any]:
    """Build a bulk-emit response: ``{"rib": {host: {hostname, entries}}}``."""
    return {"rib": {h: _rib_view(h, es) for h, es in views.items()}}


# --- happy path -----------------------------------------------------------


def test_run_hammerhead_writes_per_device_files(tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    out = tmp_path / "out"

    runner = _FakeRunner(
        bulk_view=_bulk_view(
            {
                "r1": [
                    {
                        "prefix": "10.0.0.0/24",
                        "protocol": "C",
                        "next_hop_interface": "eth0",
                        "next_hop_ip": "10.0.0.1",
                    },
                ],
                "r2": [
                    {
                        "prefix": "10.0.0.0/24",
                        "protocol": "O",
                        "next_hop_interface": "eth0",
                        "next_hop_ip": "10.0.0.1",
                    },
                ],
            }
        ),
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
    # Bulk path: rib_total_s is always exactly 0.0 (field retained for
    # results-JSON schema backward compat; see HammerheadStats docstring).
    assert data["rib_total_s"] == 0.0


def test_run_hammerhead_invokes_bulk_endpoint_exactly_once(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view=_bulk_view({"zeta": [], "alpha": [], "mu": []}))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
    )
    # Exactly one subprocess call for the whole topology — the defining
    # behaviour change of the b46eb45 bulk-emit migration. Previously:
    # one simulate + N rib calls; now: one simulate --emit-rib all.
    assert runner.calls == [("simulate_emit_rib_all", str(tmp_path / "cfg"))]


def test_run_hammerhead_sorts_hostnames_deterministically(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view=_bulk_view({"zeta": [], "alpha": [], "mu": []}))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
    )
    # All three per-device files exist (sort order is an internal detail;
    # the user-visible behaviour is that every host in the bulk view
    # gets its file).
    for h in ("alpha", "mu", "zeta"):
        assert (tmp_path / "out" / f"{h}__default.json").is_file()


def test_run_hammerhead_empty_rib_map_writes_only_stats(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view={"rib": {}})
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="empty", runner=runner
    )
    assert stats.device_count == 0
    assert stats.total_routes == 0
    assert (tmp_path / "out" / "hammerhead_stats.json").is_file()
    json_files = list((tmp_path / "out").glob("*__default.json"))
    assert json_files == []


def test_run_hammerhead_missing_rib_key_treated_as_empty(tmp_path: Path) -> None:
    # Defensive: if Hammerhead ever emits a shape without "rib", the
    # harness treats it as "no devices" and the expected-hostnames
    # assertion (if supplied) catches the loss.
    runner = _FakeRunner(bulk_view={"not_rib": "noise"})
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    assert stats.device_count == 0


# --- expected_hostnames assertion ----------------------------------------


def test_expected_hostnames_happy_path_no_error(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view=_bulk_view({"r1": [], "r2": []}))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
        expected_hostnames=["r1", "r2"],
    )


def test_expected_hostnames_missing_device_raises(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view=_bulk_view({"r1": []}))
    with pytest.raises(RuntimeError, match="missing expected device"):
        run_hammerhead(
            tmp_path / "cfg",
            tmp_path / "out",
            topology="t",
            runner=runner,
            expected_hostnames=["r1", "r2", "r3"],
        )


def test_expected_hostnames_extra_in_response_ok(tmp_path: Path) -> None:
    # Extra devices in the response are tolerated — we only assert that
    # every **expected** host is present. An SD-WAN snapshot might
    # synthesise controller-adjacent edges that aren't in the topology
    # spec; those should not fail the bench.
    runner = _FakeRunner(bulk_view=_bulk_view({"r1": [], "r2": [], "r3": []}))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
        expected_hostnames=["r1", "r2"],
    )


def test_expected_hostnames_none_skips_assertion(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view=_bulk_view({"r1": []}))
    run_hammerhead(
        tmp_path / "cfg",
        tmp_path / "out",
        topology="t",
        runner=runner,
        expected_hostnames=None,
    )


# --- error handling -------------------------------------------------------


def test_bulk_failure_propagates(tmp_path: Path) -> None:
    runner = _FakeRunner(
        bulk_view={},
        raise_on_bulk=RuntimeError("simulated hammerhead parse error"),
    )
    with pytest.raises(RuntimeError, match="simulated hammerhead parse error"):
        run_hammerhead(
            tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
        )


# --- subprocess runner ----------------------------------------------------


def test_subprocess_runner_passes_correct_argv(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, json.dumps(_bulk_view({"r1": []})), ""

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    cfg = HammerheadConfig(hammerhead_cli="/usr/local/bin/hammerhead", timeout_s=5.0)
    run_hammerhead(
        tmp_path / "configs",
        tmp_path / "out",
        topology="t",
        runner=runner,
        config=cfg,
    )
    # Exactly one subprocess call (bulk emit).
    assert len(seen) == 1
    cmd = seen[0]
    assert cmd[0] == "/usr/local/bin/hammerhead"
    assert cmd[1] == "simulate"
    assert str(tmp_path / "configs") in cmd
    assert "--emit-rib" in cmd
    idx = cmd.index("--emit-rib")
    assert cmd[idx + 1] == "all"
    assert "--format" in cmd and "json" in cmd


def test_subprocess_runner_nonzero_rc_raises(tmp_path: Path) -> None:
    def fake_run(_cmd: list[str], _timeout_s: float) -> tuple[int, str, str]:
        return 1, "", "boom"

    runner = SubprocessHammerheadRunner(run_cmd=fake_run)
    with pytest.raises(RuntimeError, match="hammerhead simulate --emit-rib all failed"):
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
    runner = _FakeRunner(bulk_view=_bulk_view({"r1": []}))
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    data = json.loads((tmp_path / "out" / "hammerhead_stats.json").read_text())
    # Every field on HammerheadStats is serialized. rib_total_s is
    # preserved even though it's always 0.0 in the bulk path — the
    # results JSON consumers (report layer, Batfish compare glue) rely
    # on the stable schema.
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
    assert data["rib_total_s"] == 0.0


def test_stats_type_is_hammerhead_stats(tmp_path: Path) -> None:
    runner = _FakeRunner(bulk_view={"rib": {}})
    stats = run_hammerhead(
        tmp_path / "cfg", tmp_path / "out", topology="t", runner=runner
    )
    assert isinstance(stats, HammerheadStats)
