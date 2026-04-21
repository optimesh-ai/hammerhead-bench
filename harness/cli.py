"""Top-level CLI entry point.

Subcommands:

- ``preflight`` — runs the host sanity checks (ex-``make preflight``).
- ``smoke`` — deploys one topology end-to-end, extracts vendor truth.
- ``bench`` / ``report`` — stubs until phase 7+ / 9.
"""

from __future__ import annotations

import json
import logging
import runpy
import sys
from pathlib import Path

import click

from harness.pipeline import run_topology
from harness.topology import load_spec

DEFAULT_SMOKE_TOPOLOGY = "bgp-ibgp-2node"
REPO_ROOT = Path(__file__).resolve().parent.parent


@click.group(help="hammerhead-bench: benchmark Hammerhead vs Batfish vs vendor truth.")
@click.version_option()
def main() -> None:
    """Entry point dispatched from pyproject.toml `[project.scripts]`."""


@main.command()
def preflight() -> None:
    """Run preflight checks. Equivalent to `make preflight`."""
    script = REPO_ROOT / "scripts" / "preflight.py"
    sys.exit(runpy.run_path(str(script), run_name="__main__").get("__exit_code__", 0))


@main.command()
@click.option(
    "--topology",
    default=DEFAULT_SMOKE_TOPOLOGY,
    show_default=True,
    help="Topology name under ./topologies/ to deploy.",
)
@click.option(
    "--results-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=REPO_ROOT / "results",
    show_default=True,
    help="Where per-topology JSON results get written.",
)
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Staging dir for rendered configs. Defaults to <results-dir>/workdir/<topology>.",
)
@click.option(
    "--keep-lab-on-failure",
    is_flag=True,
    help="Skip clab destroy if the run fails, so you can `docker exec` in and debug.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log every pipeline step to stderr.",
)
def smoke(
    topology: str,
    results_dir: Path,
    workdir: Path | None,
    keep_lab_on_failure: bool,
    verbose: bool,
) -> None:
    """One-topology end-to-end sanity run. Vendor truth only in phase 2."""
    _setup_logging(verbose)
    topo_dir = REPO_ROOT / "topologies" / topology
    if not topo_dir.is_dir():
        click.echo(f"smoke: topology dir {topo_dir} does not exist", err=True)
        sys.exit(2)

    spec = load_spec(topo_dir)
    if workdir is None:
        workdir = results_dir / "workdir" / spec.name

    click.echo(f"[smoke] topology={spec.name} workdir={workdir}")
    result = run_topology(
        spec,
        workdir=workdir,
        results_dir=results_dir,
        keep_lab_on_failure=keep_lab_on_failure,
    )

    _write_run_result(result, results_dir)
    _print_result(result)
    sys.exit(0 if result.status == "passed" else 1)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _write_run_result(result, results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "topology": result.topology,
        "status": result.status,
        "started_iso": result.started_iso,
        "finished_iso": result.finished_iso,
        "vendor_truth_path": str(result.vendor_truth_path)
        if result.vendor_truth_path
        else None,
        "error": result.error,
        "notes": result.notes,
    }
    (results_dir / f"{result.topology}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _print_result(result) -> None:
    status_word = result.status.upper()
    color = {"PASSED": "green", "FAILED": "red", "SKIPPED": "yellow"}.get(status_word, "white")
    click.echo(click.style(f"[{status_word}] {result.topology}", fg=color, bold=True))
    if result.error:
        click.echo(f"  error: {result.error}")
    for note in result.notes:
        click.echo(f"  note: {note}")
    if result.vendor_truth_path:
        click.echo(f"  vendor truth: {result.vendor_truth_path}")


@main.command()
@click.option("--max-nodes", type=int, default=None, help="Skip topologies larger than N nodes.")
def bench(max_nodes: int | None) -> None:
    """Full corpus benchmark run. Phase 7+."""
    _ = max_nodes
    click.echo("bench: phase 7+ deliverable. See README.md 'Development order'.", err=True)
    sys.exit(1)


@main.command()
def report() -> None:
    """Regenerate HTML + Markdown from an existing results/ dir. Phase 9."""
    click.echo("report: phase 9 deliverable. See README.md 'Development order'.", err=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
