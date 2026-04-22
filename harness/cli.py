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
from harness.pipeline import (
    BenchHooks,
    FrrOnlyTruthResult,
    SimOnlyAgreement,
    SimOnlyResult,
    ThreeWayAgreement,
    aggregate_sim_only,
    run_topology,
    run_topology_frr_only_truth,
    run_topology_sim_only,
)
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
    "--sim-only",
    is_flag=True,
    help=(
        "Skip containerlab deploy and vendor-truth extraction. Run Batfish + "
        "Hammerhead head-to-head on the rendered configs only. Works on any "
        "Docker-capable host (including macOS); the 3-way vendor path requires "
        "Linux containerlab."
    ),
)
@click.option(
    "--frr-only-truth",
    "frr_only_truth",
    is_flag=True,
    help=(
        "Auto-detect FRR-only topologies eligible for containerlab ground truth "
        "(pure FRR/Cumulus, <=20 nodes). Eligible topologies run the full 3-way "
        "pipeline (truth T vs Batfish B vs Hammerhead H); ineligible ones fall "
        "back to sim-only. Mutually exclusive with --sim-only. Requires Docker "
        "+ containerlab on a Linux host for the truth-bearing subset."
    ),
)
@click.option(
    "--trials",
    type=int,
    default=5,
    show_default=True,
    help=(
        "Run each simulator hook N times per topology and record per-trial "
        "wall-clocks + mean/std/min/max under results/<topology>.json "
        "`agreement.trials` + `agreement.trial_stats`. Only the simulator "
        "invocations repeat; rendering is deterministic so it runs once. "
        "Currently honoured in --sim-only mode; the 3-way vendor path "
        "(containerlab) always runs a single trial."
    ),
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
    sim_only: bool,
    frr_only_truth: bool,
    trials: int,
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

    if sim_only and frr_only_truth:
        click.echo(
            "bench: --sim-only and --frr-only-truth are mutually exclusive",
            err=True,
        )
        sys.exit(2)

    if trials < 1:
        click.echo(f"bench: --trials must be >= 1, got {trials}", err=True)
        sys.exit(2)
    if trials != 1 and not sim_only:
        click.echo(
            "bench: --trials N > 1 is only honoured with --sim-only; "
            "the 3-way vendor path always runs a single trial.",
            err=True,
        )
        sys.exit(2)

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

    if sim_only:
        _run_bench_sim_only(
            selected, hooks=hooks, results_dir=results_dir, trials=trials
        )
        return

    if frr_only_truth:
        _run_bench_frr_only_truth(
            selected, hooks=hooks, results_dir=results_dir
        )
        return

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


def _run_bench_sim_only(
    selected: list[TopologySpec],
    *,
    hooks: BenchHooks,
    results_dir: Path,
    trials: int = 1,
) -> None:
    """Sim-only bench loop: no clab, no vendor truth — Batfish vs Hammerhead only."""
    results: list[SimOnlyResult] = []
    agreements: list[SimOnlyAgreement] = []
    failed: list[str] = []

    for spec in selected:
        workdir = results_dir / "workdir" / spec.name
        click.echo(f"[bench sim-only trials={trials}] topology={spec.name}")
        try:
            result = run_topology_sim_only(
                spec,
                workdir=workdir,
                results_dir=results_dir,
                hooks=hooks,
                trials=trials,
            )
        except Exception as exc:  # noqa: BLE001 — bench catch-all
            click.echo(
                f"[bench sim-only] {spec.name}: {type(exc).__name__}: {exc}",
                err=True,
            )
            failed.append(spec.name)
            continue
        results.append(result)
        _write_sim_only_result(result, results_dir)
        _print_sim_only_result(result)
        if result.status != "passed":
            failed.append(spec.name)
        if result.agreement is not None:
            agreements.append(result.agreement)

    summary = aggregate_sim_only(agreements)
    summary["failed_topologies"] = failed
    summary["mode"] = "sim_only"
    (results_dir / "bench_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    click.echo(
        f"[bench sim-only] done. {len(selected) - len(failed)}/{len(selected)} passed. "
        f"summary -> {results_dir / 'bench_summary.json'}"
    )
    sys.exit(0 if not failed else 1)


def _run_bench_frr_only_truth(
    selected: list[TopologySpec],
    *,
    hooks: BenchHooks,
    results_dir: Path,
) -> None:
    """FRR-only-truth bench loop.

    Eligible topologies (pure FRR/Cumulus, <=20 nodes) run the 3-way pipeline;
    ineligible ones fall back to sim-only. The resulting per-topology JSON
    keeps a ``truth_source`` discriminator so the markdown renderer can
    split the table.
    """
    from harness.topology import frr_only_truth_eligible  # noqa: PLC0415

    results: list[FrrOnlyTruthResult] = []
    failed: list[str] = []
    three_way: list[ThreeWayAgreement] = []
    sim_only_agrees: list[SimOnlyAgreement] = []

    for spec in selected:
        workdir = results_dir / "workdir" / spec.name
        eligible = frr_only_truth_eligible(spec)
        click.echo(
            f"[bench frr-only-truth] topology={spec.name} "
            f"eligible={'yes' if eligible else 'no (fallback: sim-only)'}"
        )
        try:
            result = run_topology_frr_only_truth(
                spec,
                workdir=workdir,
                results_dir=results_dir,
                hooks=hooks,
            )
        except Exception as exc:  # noqa: BLE001 — bench catch-all
            click.echo(
                f"[bench frr-only-truth] {spec.name}: {type(exc).__name__}: {exc}",
                err=True,
            )
            failed.append(spec.name)
            continue
        results.append(result)
        _write_frr_only_truth_result(result, results_dir)
        _print_frr_only_truth_result(result)
        if result.status != "passed":
            failed.append(spec.name)
        if result.three_way_agreement is not None:
            three_way.append(result.three_way_agreement)
        if result.sim_only_agreement is not None:
            sim_only_agrees.append(result.sim_only_agreement)

    summary: dict = {
        "mode": "frr_only_truth",
        "topology_count": len(selected),
        "with_truth_count": len(three_way),
        "sim_only_fallback_count": len(sim_only_agrees),
        "failed_topologies": failed,
    }
    if sim_only_agrees:
        summary["sim_only_summary"] = aggregate_sim_only(sim_only_agrees)
    if three_way:
        summary["three_way_details"] = [a.as_dict() for a in three_way]
    (results_dir / "bench_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    click.echo(
        f"[bench frr-only-truth] done. "
        f"{len(selected) - len(failed)}/{len(selected)} passed; "
        f"{len(three_way)} with truth, {len(sim_only_agrees)} sim-only fallback. "
        f"summary -> {results_dir / 'bench_summary.json'}"
    )
    sys.exit(0 if not failed else 1)


def _write_frr_only_truth_result(result: FrrOnlyTruthResult, results_dir: Path) -> None:
    """Write a per-topology frr-only-truth run to ``<results_dir>/<topology>.json``.

    Carries both ``three_way_agreement`` and ``sim_only_agreement`` — exactly
    one is non-null depending on whether the topology was eligible. The
    ``truth_source`` top-level field is the primary discriminator; readers
    that don't care about the split can just index on ``agreement`` (which
    resolves to whichever is set).
    """
    results_dir.mkdir(parents=True, exist_ok=True)

    def _rel(p: Path | None) -> str | None:
        if p is None:
            return None
        try:
            return str(Path(p).resolve().relative_to(results_dir.resolve()))
        except ValueError:
            return str(p)

    if result.three_way_agreement is not None:
        agreement_payload: dict | None = result.three_way_agreement.as_dict()
    elif result.sim_only_agreement is not None:
        agreement_payload = result.sim_only_agreement.as_dict()
    else:
        agreement_payload = None

    payload = {
        "topology": result.topology,
        "status": result.status,
        "mode": "frr_only_truth",
        "truth_source": result.truth_source,
        "started_iso": result.started_iso,
        "finished_iso": result.finished_iso,
        "vendor_truth_path": _rel(result.vendor_truth_path),
        "batfish_path": _rel(result.batfish_path),
        "hammerhead_path": _rel(result.hammerhead_path),
        "diff_path": _rel(result.diff_path),
        "agreement": agreement_payload,
        "three_way_agreement": (
            result.three_way_agreement.as_dict()
            if result.three_way_agreement
            else None
        ),
        "sim_only_agreement": (
            result.sim_only_agreement.as_dict()
            if result.sim_only_agreement
            else None
        ),
        "error": result.error,
        "notes": result.notes,
    }
    (results_dir / f"{result.topology}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _print_frr_only_truth_result(result: FrrOnlyTruthResult) -> None:
    status_word = result.status.upper()
    color = {"PASSED": "green", "FAILED": "red"}.get(status_word, "white")
    suffix = f" (truth={result.truth_source})" if result.truth_source else " (sim-only)"
    click.echo(click.style(f"[{status_word}] {result.topology}{suffix}", fg=color, bold=True))
    if result.error:
        click.echo(f"  error: {result.error}")
    if result.three_way_agreement is not None:
        a = result.three_way_agreement
        click.echo(
            f"  T={a.truth_routes} B={a.batfish_routes} H={a.hammerhead_routes} routes. "
            f"B vs T nh={a.batfish_vs_truth_next_hop:.1%}, "
            f"H vs T nh={a.hammerhead_vs_truth_next_hop:.1%}, "
            f"B vs H nh={a.batfish_vs_hammerhead_next_hop:.1%}"
        )
    elif result.sim_only_agreement is not None:
        a = result.sim_only_agreement
        click.echo(
            f"  B={a.batfish_routes} H={a.hammerhead_routes} routes. "
            f"nh_agree={a.next_hop_agreement:.1%}"
        )


def _write_sim_only_result(result: SimOnlyResult, results_dir: Path) -> None:
    """Write a per-topology sim-only run result to ``<results_dir>/<topology>.json``.

    Shape diverges from the 3-way vendor-truth result so the report loader can
    distinguish the two modes by the presence of ``agreement`` vs ``metrics``.
    Paths are written relative to ``results_dir`` so committed artifacts
    don't leak operator filesystem layout.
    """
    results_dir.mkdir(parents=True, exist_ok=True)

    def _rel(p: Path | None) -> str | None:
        if p is None:
            return None
        try:
            return str(Path(p).resolve().relative_to(results_dir.resolve()))
        except ValueError:
            return str(p)

    payload = {
        "topology": result.topology,
        "status": result.status,
        "mode": "sim_only",
        "started_iso": result.started_iso,
        "finished_iso": result.finished_iso,
        "batfish_path": _rel(result.batfish_path),
        "hammerhead_path": _rel(result.hammerhead_path),
        "diff_path": _rel(result.diff_path),
        "agreement": result.agreement.as_dict() if result.agreement else None,
        "error": result.error,
        "notes": result.notes,
    }
    (results_dir / f"{result.topology}.json").write_text(json.dumps(payload, indent=2) + "\n")


def _print_sim_only_result(result: SimOnlyResult) -> None:
    status_word = result.status.upper()
    color = {"PASSED": "green", "FAILED": "red"}.get(status_word, "white")
    click.echo(click.style(f"[{status_word}] {result.topology}", fg=color, bold=True))
    if result.error:
        click.echo(f"  error: {result.error}")
    if result.agreement is not None:
        a = result.agreement
        click.echo(
            f"  batfish={a.batfish_routes} routes, hammerhead={a.hammerhead_routes} routes, "
            f"nh_agree={a.next_hop_agreement:.1%}, proto_agree={a.protocol_agreement:.1%}, "
            f"bgp_attr_agree={a.bgp_attr_agreement:.1%}"
        )
        if a.batfish_wall_s is not None and a.hammerhead_wall_s is not None:
            if a.trial_stats is not None:
                bf = a.trial_stats.get("batfish_wall_s") or {}
                hh = a.trial_stats.get("hammerhead_wall_s") or {}
                n = (a.trials or {}).get("n", 1)
                click.echo(
                    f"  wall (n={n}): "
                    f"batfish={bf.get('mean', 0.0):.2f}±{bf.get('std', 0.0):.2f}s, "
                    f"hammerhead={hh.get('mean', 0.0):.3f}±{hh.get('std', 0.0):.3f}s"
                )
            else:
                click.echo(
                    f"  wall: batfish={a.batfish_wall_s:.2f}s, "
                    f"hammerhead={a.hammerhead_wall_s:.2f}s"
                )


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

    Loads the ``TopologySpec`` to derive the expected node list and
    passes it through to ``run_hammerhead``; a bulk-emit response
    missing any expected device fails the bench loudly rather than
    silently reducing coverage.
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

    expected_hostnames: list[str] | None = None
    topo_dir = REPO_ROOT / "topologies" / topology
    if (topo_dir / "topo.py").exists():
        spec = load_spec(topo_dir)
        # Only expect hostnames for nodes that actually emit configs.
        # Containerlab `bridge` nodes (L2 plumbing, e.g. ospf-broadcast-4node's
        # `hub`) have an empty `config_template_names` tuple and are invisible
        # to Hammerhead — correctly so, since they have no routing state.
        expected_hostnames = [
            n.name for n in spec.nodes if n.adapter.config_template_names
        ]

    run_hammerhead(
        configs_dir,
        out_dir,
        topology=topology,
        config=cfg,
        expected_hostnames=expected_hostnames,
    )


if __name__ == "__main__":
    main()
