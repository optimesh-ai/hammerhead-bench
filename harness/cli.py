"""Top-level CLI entry point.

Phase 1 ships a stub that routes all real work to `make preflight`. Subsequent
phases wire `smoke`, `bench`, and `report` subcommands here.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import click


@click.group(help="hammerhead-bench: benchmark Hammerhead vs Batfish vs vendor truth.")
@click.version_option()
def main() -> None:
    """Entry point dispatched from pyproject.toml `[project.scripts]`."""


@main.command()
def preflight() -> None:
    """Run preflight checks. Equivalent to `make preflight`."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
    sys.exit(runpy.run_path(str(script), run_name="__main__").get("__exit_code__", 0))


@main.command()
def smoke() -> None:
    """One-topology end-to-end sanity run. Phase 2."""
    click.echo("smoke: phase 2 deliverable. See README.md 'Development order'.", err=True)
    sys.exit(1)


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
