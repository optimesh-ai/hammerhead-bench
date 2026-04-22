"""Diagnose why Batfish installs zero routes on the 3 empty-intersection topologies.

Renders each topology into a tempdir, stages to the Batfish snapshot layout,
starts a batfish/allinone container, then prints:

    1. Parser / init issues
    2. BGP session compatibility (is the session even considered in-table?)
    3. BGP session status (did it establish?)
    4. Route rows (what's in the main RIB?)
    5. Node properties (did Batfish see the BGP process at all?)

Usage:

    python3 tools/diagnose_empty_bf.py bgp-ibgp-2node
    python3 tools/diagnose_empty_bf.py all

Output is JSON-per-topology printed to stdout + saved under
`results/diagnosis/<topology>.json`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from harness.render import render_topology  # noqa: E402

EMPTY_TOPOLOGIES = ("bgp-ibgp-2node", "route-map-pathological", "route-reflector-6node")


def _load_spec(topology: str):
    topo_py = REPO / "topologies" / topology / "topo.py"
    spec_obj = importlib.util.spec_from_file_location(f"topo_{topology}", topo_py)
    module = importlib.util.module_from_spec(spec_obj)
    spec_obj.loader.exec_module(module)
    return module.SPEC


def _start_batfish() -> str:
    subprocess.run(
        ["docker", "rm", "-f", "bench-diagnose-bf"],
        capture_output=True,
        check=False,
    )
    p = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", "bench-diagnose-bf",
            "-p", "9997:9997", "-p", "9996:9996", "-p", "8888:8888",
            "batfish/allinone:latest",
        ],
        capture_output=True, text=True, check=True,
    )
    cid = p.stdout.strip()
    # Wait for readiness
    for _ in range(60):
        try:
            import socket  # noqa: PLC0415
            with socket.create_connection(("localhost", 9997), timeout=2.0):
                time.sleep(2.0)
                return cid
        except OSError:
            time.sleep(1.0)
    raise RuntimeError("batfish container did not become ready")


def _stop_batfish(cid: str) -> None:
    subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)


def _stage(configs_dir: Path, stage_root: Path) -> None:
    """Stage exactly the way harness.tools.batfish.run_batfish does.

    Imported lazily to avoid pulling the full batfish module into the
    diagnostic when only the staging helpers are needed.
    """
    from harness.tools.batfish import _stage_config  # noqa: PLC0415
    stage_cfg = stage_root / "configs"
    stage_cfg.mkdir(parents=True, exist_ok=True)
    for child in sorted(configs_dir.iterdir()):
        if child.is_dir():
            for candidate, kind in (
                ("frr.conf", "frr"),
                ("startup-config", "arista"),
                ("running-config", None),
            ):
                p = child / candidate
                if p.is_file():
                    _stage_config(p, stage_cfg / f"{child.name}.cfg", kind)
                    break


def _frame_to_records(answer) -> list[dict[str, Any]]:
    try:
        df = answer.frame()
        return df.to_dict(orient="records")
    except Exception as e:  # noqa: BLE001
        return [{"__error__": repr(e)}]


def diagnose(topology: str) -> dict[str, Any]:
    from pybatfish.client.session import Session  # noqa: PLC0415

    spec = _load_spec(topology)
    out_dir = REPO / "results" / "diagnosis"
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {"topology": topology}
    with tempfile.TemporaryDirectory(prefix="diag-") as workdir_s:
        workdir = Path(workdir_s)
        render_topology(spec, workdir)
        configs_dir = workdir / "configs"

        with tempfile.TemporaryDirectory(prefix="diag-bf-") as stage_s:
            stage_root = Path(stage_s)
            _stage(configs_dir, stage_root)
            s = Session(host="localhost")
            s.init_snapshot(str(stage_root), name=f"diag-{topology}", overwrite=True)

            try:
                report["init_issues"] = _frame_to_records(s.q.initIssues().answer())
            except Exception as e:  # noqa: BLE001
                report["init_issues_error"] = repr(e)

            try:
                report["parse_warning"] = _frame_to_records(
                    s.q.parseWarning().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["parse_warning_error"] = repr(e)

            try:
                report["file_parse_status"] = _frame_to_records(
                    s.q.fileParseStatus().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["file_parse_status_error"] = repr(e)

            try:
                report["bgp_session_compatibility"] = _frame_to_records(
                    s.q.bgpSessionCompatibility().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["bgp_session_compatibility_error"] = repr(e)

            try:
                report["bgp_session_status"] = _frame_to_records(
                    s.q.bgpSessionStatus().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["bgp_session_status_error"] = repr(e)

            try:
                report["bgp_process_configuration"] = _frame_to_records(
                    s.q.bgpProcessConfiguration().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["bgp_process_configuration_error"] = repr(e)

            try:
                report["bgp_peer_configuration"] = _frame_to_records(
                    s.q.bgpPeerConfiguration().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["bgp_peer_configuration_error"] = repr(e)

            try:
                report["routes_main"] = _frame_to_records(s.q.routes().answer())
            except Exception as e:  # noqa: BLE001
                report["routes_main_error"] = repr(e)

            try:
                report["bgp_rib"] = _frame_to_records(s.q.bgpRib().answer())
            except Exception as e:  # noqa: BLE001
                report["bgp_rib_error"] = repr(e)

            try:
                report["node_properties"] = _frame_to_records(
                    s.q.nodeProperties().answer()
                )
            except Exception as e:  # noqa: BLE001
                report["node_properties_error"] = repr(e)

            try:
                report["referenced_structures"] = _frame_to_records(
                    s.q.referencedStructures().answer()
                )
            except Exception as e:  # noqa: BLE001
                pass  # optional

    out_path = out_dir / f"{topology}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return report


def _summary(report: dict[str, Any]) -> None:
    topo = report["topology"]
    print(f"\n=== {topo} ===")
    ii = report.get("init_issues", [])
    pw = report.get("parse_warning", [])
    fp = report.get("file_parse_status", [])
    bsc = report.get("bgp_session_compatibility", [])
    bss = report.get("bgp_session_status", [])
    bpc = report.get("bgp_process_configuration", [])
    bp = report.get("bgp_peer_configuration", [])
    routes = report.get("routes_main", [])
    brib = report.get("bgp_rib", [])
    print(f"  init_issues rows: {len(ii)}")
    for row in ii[:5]:
        print(f"    - {row}")
    print(f"  parse_warning rows: {len(pw)}")
    for row in pw[:5]:
        print(f"    - {row}")
    print(f"  file_parse_status rows: {len(fp)}")
    for row in fp[:5]:
        print(f"    - {row}")
    print(f"  bgp_process_configuration rows: {len(bpc)}")
    for row in bpc[:3]:
        print(f"    - {row}")
    print(f"  bgp_peer_configuration rows: {len(bp)}")
    for row in bp[:5]:
        print(f"    - {row}")
    print(f"  bgp_session_compatibility rows: {len(bsc)}")
    for row in bsc[:5]:
        print(f"    - {row}")
    print(f"  bgp_session_status rows: {len(bss)}")
    for row in bss[:5]:
        print(f"    - {row}")
    print(f"  routes rows: {len(routes)}")
    for row in routes[:5]:
        print(f"    - {row}")
    print(f"  bgp_rib rows: {len(brib)}")
    for row in brib[:5]:
        print(f"    - {row}")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "all":
        targets = list(EMPTY_TOPOLOGIES)
    else:
        targets = args

    cid = _start_batfish()
    try:
        for t in targets:
            report = diagnose(t)
            _summary(report)
    finally:
        _stop_batfish(cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
