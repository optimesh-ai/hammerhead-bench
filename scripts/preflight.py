#!/usr/bin/env python3
"""Preflight checks for hammerhead-bench.

Verifies the host is ready to run benchmarks:

- Python >= 3.11
- Docker CLI + daemon reachable
- containerlab CLI on PATH
- Host RAM total / available headroom
- Container images pinned in versions.lock (FRR present-or-pullable,
  Batfish digest set to a real sha256 and not the placeholder)
- On Linux, the harness can set RLIMIT_AS

Exit code 0 on pass-or-warn, 1 on any fail, 2 on dependency import failure.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    import psutil
    from rich.console import Console
    from rich.table import Table
except ImportError:
    print(
        "preflight: Python dependencies missing. Run `make install` (or "
        "`uv sync --extra dev`) from the repo root first.",
        file=sys.stderr,
    )
    sys.exit(2)

console = Console()

Status = Literal["pass", "warn", "fail"]

# Host RAM thresholds (GiB). The benchmark needs at least 16 GiB total to run
# the larger topologies without risking OOM after Batfish (4 GiB) + clab
# containers + harness overhead. Below 6 GiB free at startup we warn loudly.
MIN_TOTAL_RAM_GB = 16
MIN_AVAIL_RAM_GB = 6


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str


def check_python() -> CheckResult:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return CheckResult("python", "pass", ver)
    return CheckResult("python", "fail", f"need >= 3.11, have {ver}")


def check_docker() -> CheckResult:
    if not shutil.which("docker"):
        return CheckResult("docker", "fail", "docker CLI not on PATH")
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult("docker", "fail", "docker info timed out; daemon stuck?")
    if proc.returncode != 0:
        err = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[0]
        return CheckResult("docker", "fail", f"daemon unreachable: {err}")
    return CheckResult("docker", "pass", f"server {proc.stdout.strip() or 'unknown'}")


def check_clab() -> CheckResult:
    clab = shutil.which("containerlab") or shutil.which("clab")
    if not clab:
        return CheckResult(
            "containerlab",
            "fail",
            "not on PATH; install from https://containerlab.dev",
        )
    try:
        proc = subprocess.run(
            [clab, "version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult("containerlab", "fail", "`clab version` timed out")
    if proc.returncode != 0:
        return CheckResult("containerlab", "fail", proc.stderr.strip() or "unknown")
    # clab prints a banner; pull the version line.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    version_line = next(
        (ln for ln in lines if "version" in ln.lower()),
        lines[0] if lines else "unknown",
    )
    return CheckResult("containerlab", "pass", version_line.strip())


def check_ram() -> CheckResult:
    vm = psutil.virtual_memory()
    total_gb = vm.total / (1024**3)
    avail_gb = vm.available / (1024**3)
    detail = f"total={total_gb:.1f}G available={avail_gb:.1f}G"
    if total_gb < MIN_TOTAL_RAM_GB:
        return CheckResult("host-ram", "fail", f"{detail} (need >= {MIN_TOTAL_RAM_GB}G)")
    if avail_gb < MIN_AVAIL_RAM_GB:
        return CheckResult(
            "host-ram",
            "warn",
            f"{detail} (free < {MIN_AVAIL_RAM_GB}G; close other apps)",
        )
    return CheckResult("host-ram", "pass", detail)


def _parse_versions_lock(root: Path) -> dict[str, str]:
    lock = root / "versions.lock"
    out: dict[str, str] = {}
    if not lock.exists():
        return out
    for raw in lock.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _image_present_locally(image: str) -> bool:
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _image_pullable(image: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "manifest", "inspect", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "manifest inspect timed out"
    if proc.returncode == 0:
        return True, "registry reachable"
    return False, (proc.stderr.strip() or proc.stdout.strip()).splitlines()[0]


def check_frr_image(versions: dict[str, str]) -> CheckResult:
    image = versions.get("FRR_IMAGE")
    if not image:
        return CheckResult("frr-image", "fail", "FRR_IMAGE unset in versions.lock")
    if _image_present_locally(image):
        return CheckResult("frr-image", "pass", f"{image} local")
    ok, msg = _image_pullable(image)
    if ok:
        return CheckResult("frr-image", "warn", f"{image} not local, pullable ({msg})")
    return CheckResult("frr-image", "fail", f"{image} neither local nor reachable: {msg}")


def check_batfish_image(versions: dict[str, str]) -> CheckResult:
    image = versions.get("BATFISH_IMAGE")
    if not image:
        return CheckResult("batfish-image", "fail", "BATFISH_IMAGE unset in versions.lock")
    if "PIN_AFTER_FIRST_PULL" in image:
        return CheckResult(
            "batfish-image",
            "warn",
            "versions.lock still has the digest placeholder; see file for pin steps",
        )
    if _image_present_locally(image):
        return CheckResult("batfish-image", "pass", f"{image} local")
    ok, msg = _image_pullable(image)
    if ok:
        return CheckResult(
            "batfish-image", "warn", f"{image} not local (will fetch on first run)"
        )
    return CheckResult("batfish-image", "fail", f"{image} not reachable: {msg}")


def check_ceos_image(versions: dict[str, str]) -> CheckResult:
    # cEOS is user-supplied (not on Docker Hub). We only check whether the tag
    # is already imported; the v1 corpus runs fine without it via `bench-fast`.
    image = versions.get("CEOS_IMAGE")
    if not image:
        return CheckResult("ceos-image", "warn", "CEOS_IMAGE unset; cEOS topologies will skip")
    if _image_present_locally(image):
        return CheckResult("ceos-image", "pass", f"{image} local")
    return CheckResult(
        "ceos-image",
        "warn",
        f"{image} not imported; import tarball or run with bench-fast",
    )


def check_rlimit() -> CheckResult:
    if platform.system() != "Linux":
        return CheckResult(
            "rlimit-as",
            "warn",
            f"{platform.system()}: RLIMIT_AS unreliable, harness will skip",
        )
    # `resource` is Linux/Unix-only; deferred import keeps Windows dev-boxes happy.
    try:
        import resource  # noqa: PLC0415
    except ImportError as e:
        return CheckResult("rlimit-as", "fail", f"import resource failed: {e}")
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    except OSError as e:
        return CheckResult("rlimit-as", "fail", str(e))
    return CheckResult("rlimit-as", "pass", f"soft={soft} hard={hard}")


def check_hammerhead_cli() -> CheckResult:
    path = os.environ.get("HAMMERHEAD_CLI")
    if not path:
        return CheckResult(
            "hammerhead-cli",
            "warn",
            "HAMMERHEAD_CLI unset; copy .env.example to .env or export it",
        )
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return CheckResult("hammerhead-cli", "fail", f"{resolved} does not exist")
    if not resolved.is_file():
        return CheckResult("hammerhead-cli", "fail", f"{resolved} is not a regular file")
    # Note: we deliberately do NOT execute it here. Preflight is read-only.
    executable = resolved.stat().st_mode & 0o111
    if not executable:
        return CheckResult("hammerhead-cli", "warn", f"{resolved} is not executable")
    return CheckResult("hammerhead-cli", "pass", str(resolved))


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    versions = _parse_versions_lock(repo_root)

    checks = [
        check_python(),
        check_docker(),
        check_clab(),
        check_ram(),
        check_frr_image(versions),
        check_batfish_image(versions),
        check_ceos_image(versions),
        check_rlimit(),
        check_hammerhead_cli(),
    ]

    table = Table(title="hammerhead-bench preflight", show_header=True, header_style="bold")
    table.add_column("check", style="cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")

    styles = {"pass": "green", "warn": "yellow", "fail": "red"}
    for c in checks:
        table.add_row(c.name, f"[{styles[c.status]}]{c.status.upper()}[/]", c.detail)
    console.print(table)

    failed = [c for c in checks if c.status == "fail"]
    warned = [c for c in checks if c.status == "warn"]
    if failed:
        console.print(
            f"[red]{len(failed)} failing check(s); fix before running benches.[/red]"
        )
        return 1
    if warned:
        console.print(f"[yellow]{len(warned)} warning(s); bench will proceed.[/yellow]")
    else:
        console.print("[green]preflight ok[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
