"""Top-level CLI entry point.

Subcommands:

- ``preflight`` — runs the host sanity checks (ex-``make preflight``).
- ``smoke`` — deploys one topology end-to-end, extracts vendor truth.
- ``bench`` — iterate every topology under ./topologies/, write per-topology
  JSON + aggregate metrics.
- ``report`` — Phase 9 stub.
"""

from __future__ import annotations

import json
import logging
import os
import runpy
import sys
from pathlib import Path

import click

from harness.diff.metrics import TopologyMetrics, aggregate_many
from harness.pipeline import BenchHooks, run_topology
from harness.tools.batfish import run_batfish
from harness.tools.hammerhead import run_hammerhead
from harness.topology import TopologySpec, load_spec

DEFAULT_SMOKE_TOPOLOGY = "bgp-ibgp-2node"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Topologies that exercise ACL forwarding semantics live here; they need
# real vendor packet-trace behaviour that only the cEOS adapter can answer.
# Gated by --with-acl-semantics so bench runs stay FRR-only by default.
ACL_SEMANTICS_TOPOLOGIES: frozenset[str] = frozenset({"acl-semantics-3node"})


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
@click.option(
    "--only",
    multiple=True,
    help="Restrict the run to these topology names (repeatable).",
)
@click.option(
    "--skip",
    multiple=True,
    help="Skip these topology names (repeatable).",
)
@click.option(
    "--with-acl-semantics",
    is_flag=True,
    help="Include the semantic ACL topology (requires the cEOS adapter).",
)
@click.option(
    "--results-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=REPO_ROOT / "results",
    show_default=True,
    help="Where per-topology JSON results get written.",
)
@click.option(
    "--no-batfish",
    is_flag=True,
    help="Skip the Batfish simulator for this run.",
)
@click.option(
    "--no-hammerhead",
    is_flag=True,
    help="Skip the Hammerhead simulator for this run.",
)
@click.option(
    "--keep-lab-on-failure",
    is_flag=True,
    help="Leave dangling containers on a failed topology for manual debugging.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log every pipeline step to stderr.",
)
def bench(
    max_nodes: int | None,
    only: tuple[str, ...],
    skip: tuple[str, ...],
    with_acl_semantics: bool,
    results_dir: Path,
    no_batfish: bool,
    no_hammerhead: bool,
    keep_lab_on_failure: bool,
    verbose: bool,
) -> None:
    """Iterate every topology under ./topologies/ and collect metrics.

    Runs strictly sequentially (no intra-run parallelism — memory guards
    assume one lab alive at a time). Writes:

    - ``results/<topology>.json``               per-topology summary
    - ``results/vendor_truth/<topology>/``      per-node vendor FIB JSON
    - ``results/batfish/<topology>/``           per-node Batfish FIB JSON
    - ``results/hammerhead/<topology>/``        per-node Hammerhead FIB JSON
    - ``results/diff/<topology>/records.json``  per-prefix diff rows
    - ``results/diff/<topology>/metrics.json``  rolled-up match rates
    - ``results/bench_summary.json``            aggregate across topologies

    Any failed topology is recorded and the run continues; the process
    exits non-zero if any topology was not passed.
    """
    _setup_logging(verbose)

    selected = _select_topologies(
        only=set(only),
        skip=set(skip),
        with_acl_semantics=with_acl_semantics,
        max_nodes=max_nodes,
    )
    if not selected:
        click.echo("bench: no topologies selected", err=True)
        sys.exit(1)

    hooks = BenchHooks(
        batfish=None if no_batfish else _default_batfish_hook,
        hammerhead=None if no_hammerhead else _default_hammerhead_hook,
    )

    results: list = []
    per_topology_metrics: list[TopologyMetrics] = []
    failed: list[str] = []

    for spec in selected:
        workdir = results_dir / "workdir" / spec.name
        click.echo(f"[bench] topology={spec.name}")
        try:
            result = run_topology(
                spec,
                workdir=workdir,
                results_dir=results_dir,
                keep_lab_on_failure=keep_lab_on_failure,
                hooks=hooks,
            )
        except Exception as exc:  # noqa: BLE001 — bench is the catch-all here.
            click.echo(f"[bench] {spec.name}: {type(exc).__name__}: {exc}", err=True)
            failed.append(spec.name)
            continue
        results.append(result)
        _write_run_result(result, results_dir)
        _print_result(result)
        if result.status != "passed":
            failed.append(spec.name)
        if result.metrics is not None:
            per_topology_metrics.append(result.metrics)

    summary = aggregate_many(per_topology_metrics)
    summary["failed_topologies"] = failed
    (results_dir / "bench_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    click.echo(
        f"[bench] done. {len(selected) - len(failed)}/{len(selected)} passed. "
        f"summary -> {results_dir / 'bench_summary.json'}"
    )
    sys.exit(0 if not failed else 1)


@main.command()
@click.option(
    "--results-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=REPO_ROOT / "results",
    show_default=True,
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=REPO_ROOT / "results" / "report",
    show_default=True,
)
def report(results_dir: Path, out_dir: Path) -> None:
    """Regenerate HTML + Markdown from an existing results/ dir."""
    from harness.report.html import render_html_report  # noqa: PLC0415
    from harness.report.markdown import render_markdown_report  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "report.md"
    html_path = out_dir / "report.html"
    render_markdown_report(results_dir, md_path)
    render_html_report(results_dir, html_path)
    click.echo(f"report: wrote {md_path}")
    click.echo(f"report: wrote {html_path}")


# ----- topology selection -------------------------------------------------


def _select_topologies(
    *,
    only: set[str],
    skip: set[str],
    with_acl_semantics: bool,
    max_nodes: int | None,
) -> list[TopologySpec]:
    topo_root = REPO_ROOT / "topologies"
    specs: list[TopologySpec] = []
    for path in sorted(topo_root.iterdir()):
        if not path.is_dir() or not (path / "topo.py").exists():
            continue
        name = path.name
        if only and name not in only:
            continue
        if name in skip:
            continue
        if name in ACL_SEMANTICS_TOPOLOGIES and not with_acl_semantics:
            continue
        spec = load_spec(path)
        if max_nodes is not None and len(spec.nodes) > max_nodes:
            continue
        specs.append(spec)
    return specs


# ----- simulator hooks ----------------------------------------------------


def _default_batfish_hook(configs_dir: Path, out_dir: Path, topology: str) -> None:
    """Default Batfish hook — wraps :func:`harness.tools.batfish.run_batfish`.

    Production path only; tests inject a fake via :class:`BenchHooks`.
    """
    run_batfish(configs_dir, out_dir, topology=topology)


def _default_hammerhead_hook(configs_dir: Path, out_dir: Path, topology: str) -> None:
    """Default Hammerhead hook — wraps :func:`harness.tools.hammerhead.run_hammerhead`.

    Honors ``$HAMMERHEAD_CLI`` + fake binary scripts via environment, so the
    offline smoke path (``$HAMMERHEAD_CLI=scripts/fake_hammerhead.sh``) works
    without a real Rust binary.
    """
    from harness.tools.hammerhead import HammerheadConfig, resolve_hammerhead_cli  # noqa: PLC0415

    cfg = HammerheadConfig(hammerhead_cli=resolve_hammerhead_cli())
    # Honor FAKE_HAMMERHEAD_SOURCE_DIR if set. The pipeline always has
    # vendor_truth/<topology>/ next to it, so default to that for offline
    # end-to-end smoke when the fake binary is on PATH.
    fake_source = os.environ.get("FAKE_HAMMERHEAD_SOURCE_DIR")
    if fake_source is None and Path(cfg.hammerhead_cli).name == "fake_hammerhead.sh":
        fake_source = str(out_dir.parent.parent / "vendor_truth" / topology)
        os.environ["FAKE_HAMMERHEAD_SOURCE_DIR"] = fake_source
    run_hammerhead(configs_dir, out_dir, topology=topology, config=cfg)


if __name__ == "__main__":
    main()
