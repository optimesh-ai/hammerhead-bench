"""Per-topology orchestrator — sequential, one topology at a time.

Phase 3 scope: render → headroom-check → deploy → converge → extract(vendor)
 → destroy → recovery-verify → teardown-verify. Writes one ``MemorySample`` per
phase to ``results/memory.jsonl``.

Phase 7 extends that scope by running Batfish + Hammerhead on the rendered
configs (out-of-band, not against live containers) and computing the
vendor-vs-simulator diffs. All three simulator hooks are injectable via
``BenchHooks`` so unit tests can swap in fakes and drive the happy path
without Docker, pybatfish, or a Rust binary on the host.

Phase 11 adds a sim-only path (:func:`run_topology_sim_only`) that skips
every step that needs a Linux-only toolchain (containerlab / netns / veth)
and instead renders configs, runs Batfish + Hammerhead on them, and
computes a Hammerhead-vs-Batfish head-to-head agreement report. Lets the
bench produce real numbers on any Docker-capable host (including macOS)
without requiring a Linux VM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.adapters.bridge import BridgeAdapter
from harness.adapters.ceos import CeosAdapter
from harness.adapters.frr import FrrAdapter
from harness.clab import ClabDriver, ClabError, DeployedLab, RealClab
from harness.diff.engine import DiffRecord, diff_fibs, load_fib_workspace
from harness.diff.metrics import TopologyMetrics, aggregate
from harness.extract.fib import NodeFib, canonicalize_node_fib
from harness.memory import (
    PHASE_POST_DEPLOY,
    PHASE_POST_TEARDOWN,
    PHASE_PRE_DEPLOY,
    PHASE_RECOVERED,
    MemoryGuardError,
    MemorySample,
    append_memory_sample,
    assert_recovered_to_baseline,
    check_headroom_before_deploy,
    sample_memory,
)
from harness.render import render_topology
from harness.topology import TopologySpec

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TopologyRunResult:
    """Outcome of one pipeline pass. Written to ``results/<topology>.json``."""

    topology: str
    status: str  # "passed" | "failed" | "skipped"
    started_iso: str
    finished_iso: str
    vendor_truth_path: Path | None = None
    batfish_path: Path | None = None
    hammerhead_path: Path | None = None
    diff_path: Path | None = None
    metrics: TopologyMetrics | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    memory_samples: list[MemorySample] = field(default_factory=list)


# Injectable simulator hooks. Each takes (configs_dir, out_dir, topology)
# and writes per-(node, vrf) JSON files into out_dir. Raising propagates
# the failure into the per-topology result without leaking a partial diff.
BatfishHook = Callable[[Path, Path, str], None]
HammerheadHook = Callable[[Path, Path, str], None]


@dataclass(slots=True)
class BenchHooks:
    """Injectable simulator hooks for Phase 7+.

    ``batfish`` and ``hammerhead`` default to ``None`` so the Phase 3 code
    path (vendor-only smoke) stays unchanged. When a hook is set, the
    pipeline runs it against the rendered ``configs/`` directory and writes
    per-(node, vrf) JSON under ``results/<sim>/<topology>/``.

    ``filter_loopback_host`` threads straight to ``diff_fibs`` so the three
    FIB sources are compared on the same footing (FRR emits /32 host routes
    for its loopbacks; Batfish doesn't; Hammerhead's Rust side follows
    Batfish's convention).
    """

    batfish: BatfishHook | None = None
    hammerhead: HammerheadHook | None = None
    filter_loopback_host: bool = True


def run_topology(  # noqa: PLR0915 — phased pipeline; refactor scheduled for phase 10
    spec: TopologySpec,
    *,
    workdir: Path,
    results_dir: Path,
    clab: ClabDriver | None = None,
    keep_lab_on_failure: bool = False,
    memory_log: Path | None = None,
    headroom_multiplier: float = 2.0,
    hooks: BenchHooks | None = None,
) -> TopologyRunResult:
    """Run one topology end-to-end. Caller guarantees sequential invocation.

    ``workdir`` gets the rendered clab YAML + per-node configs.
    ``results_dir`` gets the per-node vendor-truth FIB JSON files.
    ``memory_log`` defaults to ``results_dir/memory.jsonl``; pass an explicit
    path (or ``/dev/null``-equivalent) to override for tests.

    Memory-guard sequence:
      1. Sample pre-deploy, check headroom against sum(container caps).
      2. Deploy + converge + extract + write FIB JSON.
      3. Sample post-deploy.
      4. Destroy.
      5. Sample post-teardown.
      6. Assert recovery to baseline within slack; sample recovered.

    Any ``MemoryGuardError`` from steps 1 or 6 marks the run failed with a
    clear ``error`` line; the sample that triggered the guard is still
    appended so ``memory.jsonl`` is a complete audit trail.
    """
    started = _now_iso()
    vt_dir = results_dir / "vendor_truth" / spec.name
    bf_dir = results_dir / "batfish" / spec.name
    hh_dir = results_dir / "hammerhead" / spec.name
    diff_dir = results_dir / "diff" / spec.name
    mem_path = memory_log if memory_log is not None else results_dir / "memory.jsonl"
    hooks = hooks or BenchHooks()
    result = TopologyRunResult(
        topology=spec.name,
        status="failed",
        started_iso=started,
        finished_iso=started,
    )

    driver = clab or RealClab()
    lab: DeployedLab | None = None

    sum_caps = _sum_container_caps(spec)
    baseline: MemorySample | None = None

    try:
        baseline = sample_memory(
            topology=spec.name, phase=PHASE_PRE_DEPLOY, sum_container_limits_mb=sum_caps
        )
        _record_sample(result, baseline, mem_path)
        check_headroom_before_deploy(
            sum_caps,
            multiplier=headroom_multiplier,
            available_mb=baseline.host_available_mb,
        )

        log.info("[%s] rendering configs to %s", spec.name, workdir)
        topology_yaml = render_topology(spec, workdir)

        log.info("[%s] deploying via containerlab", spec.name)
        lab = driver.deploy(topology_yaml)

        _record_sample(
            result,
            sample_memory(
                topology=spec.name,
                phase=PHASE_POST_DEPLOY,
                sum_container_limits_mb=sum_caps,
            ),
            mem_path,
        )

        log.info("[%s] waiting for convergence", spec.name)
        _wait_convergence(spec, lab)

        log.info("[%s] extracting vendor FIBs", spec.name)
        fibs = _extract_vendor_fibs(spec, lab)

        vt_dir.mkdir(parents=True, exist_ok=True)
        _write_fibs(fibs, vt_dir)
        result.vendor_truth_path = vt_dir

        configs_dir = workdir / "configs"
        if hooks.batfish is not None:
            log.info("[%s] running batfish", spec.name)
            bf_dir.mkdir(parents=True, exist_ok=True)
            hooks.batfish(configs_dir, bf_dir, spec.name)
            result.batfish_path = bf_dir
        if hooks.hammerhead is not None:
            log.info("[%s] running hammerhead", spec.name)
            hh_dir.mkdir(parents=True, exist_ok=True)
            hooks.hammerhead(configs_dir, hh_dir, spec.name)
            result.hammerhead_path = hh_dir

        if hooks.batfish is not None or hooks.hammerhead is not None:
            log.info("[%s] computing diff", spec.name)
            metrics = _compute_diff(
                spec=spec,
                results_dir=results_dir,
                diff_dir=diff_dir,
                filter_loopback_host=hooks.filter_loopback_host,
            )
            result.metrics = metrics
            result.diff_path = diff_dir

        result.status = "passed"
    except MemoryGuardError as exc:
        result.error = f"MemoryGuardError: {exc}"
        log.error("[%s] memory guard failed: %s", spec.name, exc)
    except Exception as exc:  # noqa: BLE001 — pipeline is the top-level catch-all
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("[%s] %s", spec.name, result.error)
    finally:
        if lab is not None and (result.status == "passed" or not keep_lab_on_failure):
            try:
                driver.destroy(lab.topology_yaml)
            except ClabError as exc:
                result.notes.append(f"destroy failed: {exc}")
            _record_sample(
                result,
                sample_memory(
                    topology=spec.name,
                    phase=PHASE_POST_TEARDOWN,
                    sum_container_limits_mb=sum_caps,
                ),
                mem_path,
            )
            _verify_teardown(driver, result)
            _verify_recovery(
                spec_name=spec.name,
                baseline=baseline,
                result=result,
                mem_path=mem_path,
                sum_caps=sum_caps,
            )

        result.finished_iso = _now_iso()

    return result


# ----- memory helpers ------------------------------------------------------


def _sum_container_caps(spec: TopologySpec) -> int:
    """Sum of per-container memory caps across every node in the spec."""
    return sum(n.adapter.memory_mb for n in spec.nodes)


def _record_sample(result: TopologyRunResult, sample: MemorySample, mem_path: Path) -> None:
    """Append to in-memory result + disk jsonl in one place so they can't drift."""
    result.memory_samples.append(sample)
    append_memory_sample(mem_path, sample)


def _verify_recovery(
    *,
    spec_name: str,
    baseline: MemorySample | None,
    result: TopologyRunResult,
    mem_path: Path,
    sum_caps: int,
) -> None:
    """Poll host memory until it returns to baseline, or note the failure.

    A recovery failure does NOT flip a passed run to failed — the topology
    itself succeeded, and the dangling resource (if any) is already surfaced
    as a teardown note. But the run is marked with a "memory did not recover"
    note so the operator sees it and can clean up before the next topology.
    """
    if baseline is None:
        return
    try:
        recovered_mb = assert_recovered_to_baseline(baseline.host_available_mb)
    except MemoryGuardError as exc:
        result.notes.append(f"memory did not recover: {exc}")
        return
    _record_sample(
        result,
        MemorySample(
            topology=spec_name,
            phase=PHASE_RECOVERED,
            host_available_mb=recovered_mb,
            rss_harness_mb=baseline.rss_harness_mb,  # harness RSS is steady; reuse baseline
            sum_container_limits_mb=sum_caps,
            timestamp_iso=_now_iso(),
        ),
        mem_path,
    )


# ----- convergence / extraction helpers ------------------------------------


def _wait_convergence(spec: TopologySpec, lab: DeployedLab) -> None:
    """Per spec: wait on every node, bail the whole topology if any node times out."""
    for node in spec.nodes:
        if isinstance(node.adapter, BridgeAdapter):
            continue  # bridges are L2 plumbing, no convergence concept
        container = lab.container_name(node.name)
        if isinstance(node.adapter, FrrAdapter | CeosAdapter):
            if not node.adapter.wait_for_convergence(container):
                raise TimeoutError(f"{node.name}: did not converge within hard cap")
        else:
            raise NotImplementedError(
                f"{node.name}: adapter kind {node.adapter.kind} convergence "
                "not implemented until its phase"
            )


def _extract_vendor_fibs(spec: TopologySpec, lab: DeployedLab) -> list[NodeFib]:
    """Pull + canonicalize the FIB from every node. Returns one NodeFib per (node, vrf)."""
    fibs: list[NodeFib] = []
    for node in spec.nodes:
        if isinstance(node.adapter, BridgeAdapter):
            continue  # bridges are L2 plumbing; no FIB to extract
        container = lab.container_name(node.name)
        if isinstance(node.adapter, FrrAdapter | CeosAdapter):
            raw = node.adapter.extract_fib(container, node_name=node.name)
            fibs.extend(canonicalize_node_fib(f) for f in raw)
        else:
            raise NotImplementedError(
                f"{node.name}: adapter kind {node.adapter.kind} extraction "
                "not implemented until its phase"
            )
    return fibs


def _write_fibs(fibs: list[NodeFib], vt_dir: Path) -> None:
    """One JSON file per (node, vrf). Filename: ``<node>__<vrf>.json``."""
    for fib in fibs:
        filename = f"{fib.node}__{fib.vrf}.json"
        (vt_dir / filename).write_text(fib.model_dump_json(indent=2) + "\n")


def _verify_teardown(driver: ClabDriver, result: TopologyRunResult) -> None:
    """Fail loudly if any clab-labeled container is still around post-destroy."""
    try:
        dangling = driver.dangling_resources()
    except ClabError as exc:
        result.notes.append(f"teardown verification skipped: {exc}")
        return
    if dangling:
        result.notes.append(f"dangling clab containers after destroy: {sorted(dangling)}")


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _compute_diff(
    *,
    spec: TopologySpec,
    results_dir: Path,
    diff_dir: Path,
    filter_loopback_host: bool,
) -> TopologyMetrics:
    """Load the three FIB sources, diff them, persist records + metrics."""
    workspace = load_fib_workspace(results_dir, spec.name)
    records = diff_fibs(workspace, filter_loopback_host=filter_loopback_host)
    metrics = aggregate(spec.name, records)

    diff_dir.mkdir(parents=True, exist_ok=True)
    _write_diff_records(records, diff_dir / "records.json")
    _write_metrics(metrics, diff_dir / "metrics.json")
    return metrics


def _write_diff_records(records: list[DiffRecord], path: Path) -> None:
    import json  # noqa: PLC0415 — lazy import; non-diff runs don't need json here.

    payload = [r.as_dict() for r in records]
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_metrics(metrics: TopologyMetrics, path: Path) -> None:
    import json  # noqa: PLC0415

    path.write_text(json.dumps(metrics.as_dict(), indent=2) + "\n")


# ---- sim-only path (Phase 11) --------------------------------------------


# In-band caveat emitted alongside the Hammerhead-favoring ``asym_ratio``
# key in every ``results/<topology>.json``. Re-stated verbatim here (not
# just in the README) so a downstream script that only ingests the JSON
# can't miss it. README § 2 carries the formal definition.
ASYM_RATIO_NOTE = (
    "Historically a Hammerhead-favoring lower bound when the harness "
    "used per-device rib calls; post-b46eb45 migration to bulk emit, "
    "asym_ratio and fair_ratio converge. Kept for schema stability."
)


@dataclass(slots=True)
class SimOnlyAgreement:
    """Head-to-head agreement metrics when vendor truth is not available.

    Semantics: rows counted only when BOTH simulators carry the
    ``(node, vrf, prefix)``. "Agreement" means both sides agree on a
    field; it is explicitly NOT a correctness claim because there's no
    third-party oracle.

    When the bench runs with ``--trials N > 1``, the four wall-clock /
    simulate-time scalars collapse to the **mean** across trials, and
    two companion fields carry the raw measurements + summary stats:

    * ``trials`` — ``{"n": N, "batfish_wall_s": [..], "hammerhead_wall_s": [..],
      "batfish_simulate_s": [..], "hammerhead_simulate_s": [..]}``.
    * ``trial_stats`` — per-timing ``{mean, std, min, max}``.

    At ``trials == 1`` both fields are ``None`` and the scalar semantics
    match the pre-trials shape byte-for-byte (no consumer break).
    """

    topology: str
    batfish_routes: int
    hammerhead_routes: int
    union_keys: int
    both_sides_keys: int
    next_hop_agreement: float
    protocol_agreement: float
    bgp_attr_agreement: float
    # Node count for the topology (``len(spec.nodes)``). Stamped at run time
    # so the report renderer and the § 1 table can surface it as a column
    # without re-reading the topology YAML. ``None`` on pre-nodes sidecars.
    nodes: int | None = None
    batfish_wall_s: float | None = None
    hammerhead_wall_s: float | None = None
    batfish_simulate_s: float | None = None
    hammerhead_simulate_s: float | None = None
    # Apples-to-apples counterpart to ``batfish_simulate_s`` on the Hammerhead
    # side: time spent in ``hammerhead simulate`` plus ``hammerhead rib``
    # (both subprocesses + canonical-FIB materialization). Batfish's
    # ``query_routes_s + query_bgp_s`` already bundles dataflow with
    # result serialization, so pairing it against ``hammerhead_simulate_s``
    # alone understates Hammerhead's real denominator. The
    # ``solve_plus_materialize_ratio()`` method uses this field; the older
    # ``solve_ratio()`` method (which only uses ``hammerhead_simulate_s``)
    # is retained as a documented Hammerhead-favoring lower bound.
    hammerhead_rib_total_s: float | None = None
    hammerhead_simulate_plus_rib_s: float | None = None
    # Batfish-only architectural-cost breakdown: time spent uploading the
    # rendered snapshot into the running Batfish container before any solve
    # query fires. Hammerhead has no equivalent — it reads configs from
    # disk directly — so the field is Batfish-side only.
    batfish_init_snapshot_s: float | None = None
    trials: dict | None = None
    trial_stats: dict | None = None

    @property
    def coverage(self) -> float:
        """Jaccard overlap |B \u2229 H| / |B \u222a H|.

        0.0 when a simulator fails outright (e.g. Batfish parser crashes
        and emits zero routes) and 1.0 when both sides produce identical
        key sets. Reviewers should read :attr:`next_hop_agreement` only
        in conjunction with :attr:`coverage` \u2014 an agreement rate of
        1.0 over 0 shared rows is not a claim, it's a vacuous truth.

        This is the **presence agreement** metric per-topology; see
        :attr:`presence` for the identically-valued alias that matches
        the 3-way-truth ``presence_match_rate`` academic terminology.
        """
        if self.union_keys == 0:
            return 1.0
        return self.both_sides_keys / self.union_keys

    @property
    def presence(self) -> float:
        """Per-topology presence agreement (alias for :attr:`coverage`).

        Defined as ``|B \u2229 H| / |B \u222a H|`` where ``B`` and ``H`` are
        the sets of ``(node, vrf, prefix)`` keys produced by Batfish and
        Hammerhead on the topology. This is the sim-only analogue of the
        ``presence_match_rate`` metric the 3-way-truth path reports
        against vendor ground truth (see ``harness/diff/metrics.py``).
        Surfaced as its own field so README \u00a7 2 can formalise the
        definition under a stable key instead of piggy-backing on the
        implementation-named ``coverage``.
        """
        return self.coverage

    def solve_ratio(self) -> float | None:
        """``batfish_simulate_s / hammerhead_simulate_s`` — asymmetric
        Hammerhead-favoring lower bound; do **not** use as the headline.

        Batfish's ``query_routes_s + query_bgp_s`` numerator bundles
        dataflow with result materialization (it's the entire
        pybatfish REST round-trip). Hammerhead's ``simulate_s``
        denominator is the ``hammerhead simulate`` subprocess **only**
        — it does not include the ``hammerhead rib`` subprocess that
        pybatfish's result-materialization step is analogous to.
        Pairing them produces a ratio that flatters Hammerhead at
        scale (the gap widens as the RIB grows). Use
        :meth:`solve_plus_materialize_ratio` for the fair number.

        None when either sidecar stat is missing or
        ``hammerhead_simulate_s`` is zero.
        """
        if self.batfish_simulate_s is None or self.hammerhead_simulate_s is None:
            return None
        if self.hammerhead_simulate_s <= 0:
            return None
        return self.batfish_simulate_s / self.hammerhead_simulate_s

    def solve_plus_materialize_ratio(self) -> float | None:
        """``batfish_simulate_s / (hammerhead_simulate_s + hammerhead_rib_total_s)``.

        The **headline** solver speedup: both sides include their inner
        solver work **and** result materialization. On the Batfish side
        pybatfish's ``query_routes`` / ``query_bgp`` is a fused
        dataflow-plus-output call; the matching denominator on the
        Hammerhead side is ``simulate`` (solver) + ``rib`` (per-device
        RIB extraction + canonical-FIB write). Using this ratio as the
        headline keeps the benchmark survivable under reviewer
        scrutiny — every §1 cell is "equivalent work on each side,
        wall-clock."

        Also exposed in the results JSON under the canonical key
        ``fair_ratio`` (README § 2 formal definition); ``solve_ratio``
        /``asym_ratio`` are the Hammerhead-favoring lower-bound
        counterpart and must not be cited as headline.

        None when either the Batfish sidecar stat or the Hammerhead
        combined denominator is missing or zero.
        """
        if self.batfish_simulate_s is None:
            return None
        if self.hammerhead_simulate_plus_rib_s is None:
            return None
        if self.hammerhead_simulate_plus_rib_s <= 0:
            return None
        return self.batfish_simulate_s / self.hammerhead_simulate_plus_rib_s

    def wall_ratio(self) -> float | None:
        """``batfish_wall_s / hammerhead_wall_s`` — end-to-end wall-clock ratio.

        Numerator is the full Batfish path: JVM cold-start + pybatfish
        init + snapshot upload + solve + result materialization.
        Denominator is the full Hammerhead path: ``simulate`` +
        ``rib`` subprocess spawn + JSON parse + disk write. This is the
        **conservative-upper-bound** ratio: at small topologies the
        numerator is dominated by one-time JVM startup, so the ratio
        overstates the true solver speedup. Always reported alongside
        ``fair_ratio`` in the § 1 table.
        """
        if self.batfish_wall_s is None or self.hammerhead_wall_s is None:
            return None
        if self.hammerhead_wall_s <= 0:
            return None
        return self.batfish_wall_s / self.hammerhead_wall_s

    def as_dict(self) -> dict:
        from dataclasses import asdict  # noqa: PLC0415

        d = asdict(self)
        d["coverage"] = self.coverage
        d["presence"] = self.presence
        # Canonical keys (README § 2 formal definitions). ``fair_ratio``
        # is the headline; ``asym_ratio`` is the Hammerhead-favoring
        # lower bound and carries an in-band caveat so a downstream
        # consumer reading only the JSON can't miss it.
        d["wall_ratio"] = self.wall_ratio()
        d["fair_ratio"] = self.solve_plus_materialize_ratio()
        d["asym_ratio"] = self.solve_ratio()
        d["asym_ratio_note"] = ASYM_RATIO_NOTE
        # Legacy aliases — retained so pre-rename result consumers keep
        # parsing. New consumers should read the canonical keys above.
        d["solve_ratio"] = d["asym_ratio"]
        d["solve_plus_materialize_ratio"] = d["fair_ratio"]
        return d


@dataclass(slots=True)
class SimOnlyResult:
    """Outcome of a sim-only pipeline pass. Written to ``results/<topology>.json``."""

    topology: str
    status: str  # "passed" | "failed"
    started_iso: str
    finished_iso: str
    batfish_path: Path | None = None
    hammerhead_path: Path | None = None
    diff_path: Path | None = None
    agreement: SimOnlyAgreement | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def run_topology_sim_only(
    spec: TopologySpec,
    *,
    workdir: Path,
    results_dir: Path,
    hooks: BenchHooks | None = None,
    trials: int = 1,
) -> SimOnlyResult:
    """Render configs, run Batfish + Hammerhead, diff them head-to-head.

    No containerlab, no vendor truth, no memory guards — this path assumes
    both simulators are pure shell-out + parse operations. Output layout:

    - ``<results_dir>/batfish/<topology>/<node>__<vrf>.json`` — Batfish FIB
    - ``<results_dir>/hammerhead/<topology>/<node>__<vrf>.json`` — Hammerhead FIB
    - ``<results_dir>/diff_sim_only/<topology>/agreement.json`` — head-to-head

    ``hooks.batfish`` and ``hooks.hammerhead`` are required. An unset hook
    makes the pipeline write an empty FIB dir for that side; the agreement
    metrics handle that gracefully (presence goes to zero).

    ``trials`` controls how many times each simulator hook fires. Rendering
    is deterministic so it runs once; only the hook invocations (and their
    sidecar stats) are repeated. Agreement is computed from the final
    trial's FIB output (all trials must produce identical routes or the
    simulator is non-deterministic — a separate concern). Per-trial wall
    + ``*_simulate_s`` timings are collected into ``agreement.trials`` and
    summarised to ``mean/std/min/max`` in ``agreement.trial_stats``.
    """
    import time as _time  # noqa: PLC0415

    if trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")

    started = _now_iso()
    bf_dir = results_dir / "batfish" / spec.name
    hh_dir = results_dir / "hammerhead" / spec.name
    diff_dir = results_dir / "diff_sim_only" / spec.name
    hooks = hooks or BenchHooks()
    result = SimOnlyResult(
        topology=spec.name,
        status="failed",
        started_iso=started,
        finished_iso=started,
    )

    try:
        log.info(
            "[%s] rendering configs to %s (sim-only, trials=%d)",
            spec.name,
            workdir,
            trials,
        )
        render_topology(spec, workdir)
        configs_dir = workdir / "configs"

        batfish_walls: list[float] = []
        batfish_sims: list[float] = []
        batfish_inits: list[float] = []
        hammer_walls: list[float] = []
        hammer_sims: list[float] = []
        hammer_ribs: list[float] = []

        if hooks.batfish is not None:
            bf_dir.mkdir(parents=True, exist_ok=True)
            result.batfish_path = bf_dir
        if hooks.hammerhead is not None:
            hh_dir.mkdir(parents=True, exist_ok=True)
            result.hammerhead_path = hh_dir

        for i in range(trials):
            if hooks.batfish is not None:
                log.info("[%s] running batfish (trial %d/%d)", spec.name, i + 1, trials)
                t0 = _time.monotonic()
                hooks.batfish(configs_dir, bf_dir, spec.name)
                wall = _time.monotonic() - t0
                batfish_walls.append(wall)
                sidecar = bf_dir / "batfish_stats.json"
                sim_s = _read_stat(sidecar, "simulate_s")
                init_s = _read_stat(sidecar, "init_snapshot_s")
                total_s = _read_stat(sidecar, "total_s")
                _assert_simulate_le_total("batfish", spec.name, sim_s, total_s)
                if sim_s is not None:
                    batfish_sims.append(sim_s)
                if init_s is not None:
                    batfish_inits.append(init_s)
            if hooks.hammerhead is not None:
                log.info("[%s] running hammerhead (trial %d/%d)", spec.name, i + 1, trials)
                t0 = _time.monotonic()
                hooks.hammerhead(configs_dir, hh_dir, spec.name)
                wall = _time.monotonic() - t0
                hammer_walls.append(wall)
                sidecar = hh_dir / "hammerhead_stats.json"
                sim_s = _read_stat(sidecar, "simulate_s")
                rib_s = _read_stat(sidecar, "rib_total_s")
                total_s = _read_stat(sidecar, "total_s")
                _assert_simulate_le_total("hammerhead", spec.name, sim_s, total_s)
                _assert_simulate_le_total(
                    "hammerhead (simulate+rib)",
                    spec.name,
                    (sim_s or 0.0) + (rib_s or 0.0) if sim_s is not None or rib_s is not None else None,
                    total_s,
                )
                if sim_s is not None:
                    hammer_sims.append(sim_s)
                if rib_s is not None:
                    hammer_ribs.append(rib_s)

        agreement = _compute_sim_only_agreement(
            spec=spec,
            results_dir=results_dir,
            diff_dir=diff_dir,
            filter_loopback_host=hooks.filter_loopback_host,
            batfish_walls=batfish_walls,
            hammerhead_walls=hammer_walls,
            batfish_sims=batfish_sims,
            hammerhead_sims=hammer_sims,
            batfish_inits=batfish_inits,
            hammerhead_ribs=hammer_ribs,
            # Count only config-emitting nodes. Containerlab bridge adapters
            # (empty config_template_names) are L2 plumbing and don't
            # participate in routing; including them overcounts the
            # control-plane topology size on ospf-broadcast-4node /
            # route-reflector-6node, where a `hub` bridge sits alongside the
            # routers.
            nodes=sum(1 for n in spec.nodes if n.adapter.config_template_names),
        )
        result.agreement = agreement
        result.diff_path = diff_dir
        result.status = "passed"
    except Exception as exc:  # noqa: BLE001 — sim-only is the top-level catch-all here
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("[%s] sim-only failed: %s", spec.name, result.error)
    finally:
        result.finished_iso = _now_iso()
    return result


def _compute_sim_only_agreement(
    *,
    spec: TopologySpec,
    results_dir: Path,
    diff_dir: Path,
    filter_loopback_host: bool,
    batfish_walls: list[float],
    hammerhead_walls: list[float],
    batfish_sims: list[float],
    hammerhead_sims: list[float],
    batfish_inits: list[float] | None = None,
    hammerhead_ribs: list[float] | None = None,
    nodes: int | None = None,
) -> SimOnlyAgreement:
    """Diff Batfish and Hammerhead head-to-head; write records + agreement.json.

    ``batfish_walls`` etc. hold per-trial measurements; each scalar field on
    the returned :class:`SimOnlyAgreement` is the arithmetic mean across
    the list, with the raw values + summary stats preserved under
    ``trials`` / ``trial_stats``.
    """
    import json  # noqa: PLC0415

    workspace = load_fib_workspace(results_dir, spec.name)
    # Re-index directly; the existing diff engine treats one of the two sides
    # as vendor truth, which would mislabel the result here.
    batfish_ix = _sim_only_index(workspace.batfish, filter_loopback_host)
    hammer_ix = _sim_only_index(workspace.hammerhead, filter_loopback_host)

    union_keys = set(batfish_ix) | set(hammer_ix)
    both_keys = set(batfish_ix) & set(hammer_ix)

    nh_agree = 0
    proto_agree = 0
    bgp_total = 0
    bgp_agree = 0
    records: list[dict] = []
    for key in sorted(union_keys, key=lambda k: (k.node, k.vrf, k.prefix)):
        b = batfish_ix.get(key)
        h = hammer_ix.get(key)
        row: dict = {
            "node": key.node,
            "vrf": key.vrf,
            "prefix": key.prefix,
            "in_batfish": b is not None,
            "in_hammerhead": h is not None,
            "batfish_protocol": b.protocol if b else None,
            "hammerhead_protocol": h.protocol if h else None,
        }
        if b is not None and h is not None:
            row["next_hop_agree"] = _nh_sets_equal_sim_only(b, h)
            row["protocol_agree"] = b.protocol == h.protocol
            if b.protocol == "bgp" and h.protocol == "bgp":
                row["bgp_attrs_agree"] = (
                    _as_path_equal_sim_only(b.as_path, h.as_path)
                    and b.local_pref == h.local_pref
                    and b.med == h.med
                )
            else:
                row["bgp_attrs_agree"] = None
            if row["next_hop_agree"]:
                nh_agree += 1
            if row["protocol_agree"]:
                proto_agree += 1
            if row["bgp_attrs_agree"] is not None:
                bgp_total += 1
                if row["bgp_attrs_agree"] is True:
                    bgp_agree += 1
        records.append(row)

    denom_both = len(both_keys) or 1

    bf_wall_mean = _mean_or_none(batfish_walls)
    hh_wall_mean = _mean_or_none(hammerhead_walls)
    bf_sim_mean = _mean_or_none(batfish_sims)
    hh_sim_mean = _mean_or_none(hammerhead_sims)
    bf_init_mean = _mean_or_none(batfish_inits or [])
    hh_rib_mean = _mean_or_none(hammerhead_ribs or [])
    # Paired per-trial sum, meaned afterwards — keeps the denominator
    # internally consistent when the two lists have different lengths
    # (e.g. a sidecar lacks one of the keys). Falls back to the sum of
    # means when pairing isn't possible.
    hh_sim_plus_rib_mean: float | None
    if hammerhead_sims and hammerhead_ribs and len(hammerhead_sims) == len(hammerhead_ribs):
        hh_sim_plus_rib_mean = _mean_or_none(
            [s + r for s, r in zip(hammerhead_sims, hammerhead_ribs, strict=True)]
        )
    elif hh_sim_mean is not None and hh_rib_mean is not None:
        hh_sim_plus_rib_mean = hh_sim_mean + hh_rib_mean
    elif hh_sim_mean is not None and hh_rib_mean is None:
        # Legacy sidecar without rib_total_s — report simulate-only rather
        # than synthesising a fake rib time.
        hh_sim_plus_rib_mean = hh_sim_mean
    else:
        hh_sim_plus_rib_mean = None

    trials_payload: dict | None = None
    stats_payload: dict | None = None
    # Only surface the richer shape when at least one side saw repeated
    # trials — keeps trials=1 bench_summary.json byte-for-byte compatible
    # with the pre-trials consumer.
    n_trials = max(
        len(batfish_walls),
        len(hammerhead_walls),
        len(batfish_sims),
        len(hammerhead_sims),
    )
    if n_trials >= 2:
        # Paired per-trial sums for the fair-ratio denominator, so
        # ``trial_stats["hammerhead_simulate_plus_rib_s"]`` reflects the
        # real per-trial mean/std (not a synthetic sum of independent
        # means). Falls back to sim-only when rib sidecars are missing.
        if (
            hammerhead_sims
            and hammerhead_ribs
            and len(hammerhead_sims) == len(hammerhead_ribs)
        ):
            hh_sim_plus_rib_series: list[float] = [
                s + r
                for s, r in zip(hammerhead_sims, hammerhead_ribs, strict=True)
            ]
        else:
            hh_sim_plus_rib_series = list(hammerhead_sims)
        trials_payload = {
            "n": n_trials,
            "batfish_wall_s": list(batfish_walls),
            "hammerhead_wall_s": list(hammerhead_walls),
            "batfish_simulate_s": list(batfish_sims),
            "hammerhead_simulate_s": list(hammerhead_sims),
            "hammerhead_rib_total_s": list(hammerhead_ribs or []),
            "hammerhead_simulate_plus_rib_s": hh_sim_plus_rib_series,
            "batfish_init_snapshot_s": list(batfish_inits or []),
        }
        stats_payload = {
            "batfish_wall_s": _summarize_timings(batfish_walls),
            "hammerhead_wall_s": _summarize_timings(hammerhead_walls),
            "batfish_simulate_s": _summarize_timings(batfish_sims),
            "hammerhead_simulate_s": _summarize_timings(hammerhead_sims),
            "hammerhead_rib_total_s": _summarize_timings(hammerhead_ribs or []),
            "hammerhead_simulate_plus_rib_s": _summarize_timings(
                hh_sim_plus_rib_series
            ),
            "batfish_init_snapshot_s": _summarize_timings(batfish_inits or []),
        }

    agreement = SimOnlyAgreement(
        topology=spec.name,
        batfish_routes=len(batfish_ix),
        hammerhead_routes=len(hammer_ix),
        union_keys=len(union_keys),
        both_sides_keys=len(both_keys),
        next_hop_agreement=nh_agree / denom_both if both_keys else 1.0,
        protocol_agreement=proto_agree / denom_both if both_keys else 1.0,
        bgp_attr_agreement=bgp_agree / bgp_total if bgp_total else 1.0,
        nodes=nodes,
        batfish_wall_s=bf_wall_mean,
        hammerhead_wall_s=hh_wall_mean,
        batfish_simulate_s=bf_sim_mean,
        hammerhead_simulate_s=hh_sim_mean,
        hammerhead_rib_total_s=hh_rib_mean,
        hammerhead_simulate_plus_rib_s=hh_sim_plus_rib_mean,
        batfish_init_snapshot_s=bf_init_mean,
        trials=trials_payload,
        trial_stats=stats_payload,
    )

    diff_dir.mkdir(parents=True, exist_ok=True)
    (diff_dir / "records.json").write_text(json.dumps(records, indent=2) + "\n")
    (diff_dir / "agreement.json").write_text(json.dumps(agreement.as_dict(), indent=2) + "\n")
    return agreement


def _mean_or_none(xs: list[float]) -> float | None:
    """Arithmetic mean of ``xs``; ``None`` for empty list (simulator skipped)."""
    if not xs:
        return None
    return sum(xs) / len(xs)


def _summarize_timings(xs: list[float]) -> dict[str, float] | None:
    """``{mean, std, min, max}`` over ``xs`` (population stddev; 0.0 for len==1).

    Returns ``None`` when ``xs`` is empty so the caller can distinguish
    "simulator was skipped" from "simulator ran but produced no samples".
    """
    if not xs:
        return None
    import statistics  # noqa: PLC0415 — only needed on the trials path

    mean = statistics.fmean(xs)
    # Sample stddev (n-1) for trials >=2; 0.0 for single-trial so downstream
    # "mean ± std" rendering doesn't NaN out.
    std = statistics.stdev(xs) if len(xs) >= 2 else 0.0
    return {
        "mean": mean,
        "std": std,
        "min": min(xs),
        "max": max(xs),
    }


def _sim_only_index(fibs, filter_loopback_host: bool):
    """Index FIBs by (node, vrf, prefix) → Route. Mirrors the private helper in engine.py."""
    from harness.diff.engine import _RouteKey  # noqa: PLC0415
    from harness.extract.fib import canonicalize_node_fib  # noqa: PLC0415

    out = {}
    for raw in fibs:
        fib = canonicalize_node_fib(raw, filter_loopback_host=filter_loopback_host)
        for r in fib.routes:
            out[_RouteKey(node=fib.node, vrf=fib.vrf, prefix=r.prefix)] = r
    return out


def _nh_sets_equal_sim_only(a, b) -> bool:
    """Forwarding-equivalent next-hop equality.

    Interface names differ gratuitously between simulators (``Loopback`` vs
    ``lo``; ``dynamic`` vs ``None`` for BGP recursive next-hops), so we
    compare on the semantically-meaningful axis: the set of next-hop IPs.
    When both sides have at least one IP, IP-set equality wins; otherwise we
    fall back to the interface-name set so pure-connected routes (no IP) are
    still checked.
    """
    a_ips = frozenset(n.ip for n in a.next_hops if n.ip is not None)
    b_ips = frozenset(n.ip for n in b.next_hops if n.ip is not None)
    if a_ips and b_ips:
        return a_ips == b_ips
    # Pure-connected / pure-local routes carry no IP next-hop. Interface
    # names are vendor/simulator-labeled and differ gratuitously
    # (``Loopback`` vs ``lo``, ``GigabitEthernet0/0/0/0`` vs ``eth1``).
    # When both sides report at least one interface next-hop for the same
    # prefix with agreeing protocol, treat as forwarding-equivalent — the
    # presence + protocol signal is the honest outcome.
    a_has_iface = any(n.interface for n in a.next_hops)
    b_has_iface = any(n.interface for n in b.next_hops)
    if not a_ips and not b_ips and a_has_iface and b_has_iface:
        return True
    if not a_ips and not b_ips and not a_has_iface and not b_has_iface:
        return True
    return False


def _as_path_equal_sim_only(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a == b


def _read_stat(path: Path, key: str) -> float | None:
    """Read a single float key out of a sidecar stats JSON; None if absent."""
    import json  # noqa: PLC0415

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    val = data.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# 100ms tolerance: measurement noise between the outer monotonic and the
# inner sidecar stamp is single-digit ms on every host we've seen; anything
# larger means ``simulate_s`` has been mis-aliased to a wall-clock stat
# (the 2026-04-22 regression we just unwound). The check only fires when
# both values are present so it doesn't trip on legacy sidecars without
# a separate ``simulate_s`` key.
_SIMULATE_LE_TOTAL_TOLERANCE_S = 0.1


def _assert_simulate_le_total(
    tool: str, topology: str, simulate_s: float | None, total_s: float | None
) -> None:
    """Guardrail: ``simulate_s`` must never exceed ``total_s`` by more than 100 ms.

    A violation means the inner-solver field has been re-aliased to the
    full wall-clock — the exact bug we just fixed. Raises ``RuntimeError``
    at benchmark-time instead of waiting for a reviewer to notice that
    "wall ≈ solve" in the report.
    """
    if simulate_s is None or total_s is None:
        return
    if simulate_s > total_s + _SIMULATE_LE_TOTAL_TOLERANCE_S:
        raise RuntimeError(
            f"[{topology}] {tool}: simulate_s ({simulate_s:.6f}s) exceeds "
            f"total_s ({total_s:.6f}s) by more than "
            f"{_SIMULATE_LE_TOTAL_TOLERANCE_S:.3f}s — inner-solver field "
            "is probably aliased to a wall-clock stat; check the wrapper."
        )


# ---- FRR-only ground-truth path (Issue 4) --------------------------------


@dataclass(slots=True)
class ThreeWayAgreement:
    """Three-way agreement (vendor truth T ↔ Batfish B ↔ Hammerhead H).

    Only materialised by ``run_topology_frr_only_truth`` when the topology is
    eligible for containerlab ground-truth collection (FRR / Cumulus only,
    ≤ :data:`harness.topology.FRR_ONLY_TRUTH_MAX_NODES` nodes). Non-eligible
    topologies fall back to the plain :class:`SimOnlyAgreement` shape with
    ``truth_source == None`` so a single corpus-level result JSON can mix
    truth and sim-only rows without a schema flag.

    Field semantics:

    * ``truth_routes`` — count of ``(node, vrf, prefix)`` keys in the vendor
      FIB for this topology (collected via ``FrrAdapter.extract_fib``).
    * ``*_vs_*_{next_hop,protocol,bgp_attr}`` — fraction of cells in the
      intersection of the two sides where the relation holds.
      Denominators are set-size-independent per pair: the denominator is
      ``|X ∩ Y|`` for ``X_vs_Y_next_hop`` etc. See ``harness/diff/metrics.py``
      for the exact arithmetic (reused verbatim, 1.0 on empty-intersection).
    * ``*_vs_*_presence`` — Jaccard ``|X ∩ Y| / |X ∪ Y|``.
    * ``*_vs_*_coverage`` — alias for ``presence`` so the JSON shape matches
      the per-topology sim-only agreement (lets the report table reuse the
      same column helpers).
    * ``truth_source`` — always ``"containerlab-frr"`` for this dataclass.
      Non-eligible topologies set the attribute to ``None`` on the enclosing
      run result (not on this object, which only exists when truth ran).

    The B ↔ H triad (``batfish_vs_hammerhead_*``) re-exposes the sim-only
    agreement metrics so downstream consumers can index on one dataclass
    rather than juggling two.
    """

    topology: str
    truth_source: str = "containerlab-frr"

    # Raw counts + timing
    truth_routes: int = 0
    batfish_routes: int = 0
    hammerhead_routes: int = 0
    truth_simulate_s: float | None = None
    batfish_wall_s: float | None = None
    hammerhead_wall_s: float | None = None
    batfish_simulate_s: float | None = None
    hammerhead_simulate_s: float | None = None
    # See :class:`SimOnlyAgreement` for field semantics. Surfaced here
    # so three-way truth rows can report the fair
    # ``solve_plus_materialize_ratio`` alongside the asymmetric
    # ``solve_ratio``.
    hammerhead_rib_total_s: float | None = None
    hammerhead_simulate_plus_rib_s: float | None = None

    # B vs T
    batfish_vs_truth_both_keys: int = 0
    batfish_vs_truth_union_keys: int = 0
    batfish_vs_truth_presence: float = 1.0
    batfish_vs_truth_next_hop: float = 1.0
    batfish_vs_truth_protocol: float = 1.0
    batfish_vs_truth_bgp_attr: float = 1.0

    # H vs T
    hammerhead_vs_truth_both_keys: int = 0
    hammerhead_vs_truth_union_keys: int = 0
    hammerhead_vs_truth_presence: float = 1.0
    hammerhead_vs_truth_next_hop: float = 1.0
    hammerhead_vs_truth_protocol: float = 1.0
    hammerhead_vs_truth_bgp_attr: float = 1.0

    # B vs H (re-exported from the sim-only agreement shape)
    batfish_vs_hammerhead_both_keys: int = 0
    batfish_vs_hammerhead_union_keys: int = 0
    batfish_vs_hammerhead_presence: float = 1.0
    batfish_vs_hammerhead_next_hop: float = 1.0
    batfish_vs_hammerhead_protocol: float = 1.0
    batfish_vs_hammerhead_bgp_attr: float = 1.0

    def as_dict(self) -> dict:
        from dataclasses import asdict  # noqa: PLC0415

        d = asdict(self)
        # Presence-aliased keys so the markdown renderer can treat the
        # three triads uniformly.
        d["batfish_vs_truth_coverage"] = self.batfish_vs_truth_presence
        d["hammerhead_vs_truth_coverage"] = self.hammerhead_vs_truth_presence
        d["batfish_vs_hammerhead_coverage"] = self.batfish_vs_hammerhead_presence
        # Canonical ratio keys — same shape as
        # :meth:`SimOnlyAgreement.as_dict` so a downstream tool indexes
        # the same key regardless of whether the topology carried truth.
        wall_ratio = (
            self.batfish_wall_s / self.hammerhead_wall_s
            if self.batfish_wall_s is not None
            and self.hammerhead_wall_s is not None
            and self.hammerhead_wall_s > 0
            else None
        )
        fair_ratio = (
            self.batfish_simulate_s / self.hammerhead_simulate_plus_rib_s
            if self.batfish_simulate_s is not None
            and self.hammerhead_simulate_plus_rib_s is not None
            and self.hammerhead_simulate_plus_rib_s > 0
            else None
        )
        asym_ratio = (
            self.batfish_simulate_s / self.hammerhead_simulate_s
            if self.batfish_simulate_s is not None
            and self.hammerhead_simulate_s is not None
            and self.hammerhead_simulate_s > 0
            else None
        )
        d["wall_ratio"] = wall_ratio
        d["fair_ratio"] = fair_ratio
        d["asym_ratio"] = asym_ratio
        d["asym_ratio_note"] = ASYM_RATIO_NOTE
        d["solve_ratio"] = asym_ratio
        d["solve_plus_materialize_ratio"] = fair_ratio
        return d


@dataclass(slots=True)
class FrrOnlyTruthResult:
    """Outcome of one ``run_topology_frr_only_truth`` pass.

    The container can carry either a :class:`ThreeWayAgreement` (topology was
    eligible + truth collection succeeded) or a :class:`SimOnlyAgreement`
    (topology was ineligible for truth → fell back to sim-only). The
    ``truth_source`` attribute is the single discriminator downstream
    consumers should check.
    """

    topology: str
    status: str  # "passed" | "failed"
    started_iso: str
    finished_iso: str
    truth_source: str | None = None  # "containerlab-frr" | None (sim-only fallback)
    vendor_truth_path: Path | None = None
    batfish_path: Path | None = None
    hammerhead_path: Path | None = None
    diff_path: Path | None = None
    three_way_agreement: ThreeWayAgreement | None = None
    sim_only_agreement: SimOnlyAgreement | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


# Type alias: a callable that, given (spec, workdir, results_dir), writes one
# ``<node>__<vrf>.json`` per (node, vrf) under
# ``results_dir/vendor_truth/<topology>/`` and returns nothing. The production
# path uses :func:`_default_truth_collector` (clab deploy + FRR adapter); tests
# inject a fake that stamps out hand-built FIBs.
TruthCollector = Callable[[TopologySpec, Path, Path], None]


def _default_truth_collector(
    spec: TopologySpec,
    workdir: Path,
    results_dir: Path,
) -> None:
    """Production truth collector: clab deploy → converge → extract → destroy.

    Re-uses the existing :func:`run_topology` machinery so the container /
    convergence / extraction logic stays in one place. Raises on any failure
    so ``run_topology_frr_only_truth`` can fall back or propagate.

    Only called on Linux (checked by the caller — macOS falls back to
    sim-only). Containerlab + Docker must be on ``PATH``.
    """
    result = run_topology(
        spec,
        workdir=workdir,
        results_dir=results_dir,
        hooks=BenchHooks(),  # no batfish/hammerhead yet — those run later
    )
    if result.status != "passed":
        raise RuntimeError(
            f"truth collection failed for {spec.name}: {result.error}"
        )


def run_topology_frr_only_truth(
    spec: TopologySpec,
    *,
    workdir: Path,
    results_dir: Path,
    hooks: BenchHooks | None = None,
    truth_collector: TruthCollector | None = None,
) -> FrrOnlyTruthResult:
    """Run one topology under the ``--frr-only-truth`` pipeline.

    Semantics:

    * If :func:`harness.topology.frr_only_truth_eligible` returns ``False``,
      fall back to :func:`run_topology_sim_only` and record
      ``truth_source = None`` in the result.
    * Otherwise, collect vendor truth via ``truth_collector`` (default:
      :func:`_default_truth_collector` — containerlab-backed),
      run Batfish + Hammerhead on the same rendered configs, and compute
      the three-way agreement triad (B↔T, H↔T, B↔H).

    Tests pass a mock ``truth_collector`` that stamps out hand-built FIBs
    directly into ``results_dir/vendor_truth/<topology>/``; the real one
    requires Docker + containerlab on a Linux host.
    """
    from harness.topology import frr_only_truth_eligible  # noqa: PLC0415

    started = _now_iso()
    hooks = hooks or BenchHooks()

    if not frr_only_truth_eligible(spec):
        # Fall back: run the sim-only path, wrap the result so the shape is
        # uniform across eligible / ineligible topologies.
        sim_result = run_topology_sim_only(
            spec,
            workdir=workdir,
            results_dir=results_dir,
            hooks=hooks,
        )
        return FrrOnlyTruthResult(
            topology=spec.name,
            status=sim_result.status,
            started_iso=sim_result.started_iso,
            finished_iso=sim_result.finished_iso,
            truth_source=None,
            batfish_path=sim_result.batfish_path,
            hammerhead_path=sim_result.hammerhead_path,
            diff_path=sim_result.diff_path,
            sim_only_agreement=sim_result.agreement,
            error=sim_result.error,
            notes=list(sim_result.notes),
        )

    # Eligible path: collect truth + run simulators + three-way diff.
    import time as _time  # noqa: PLC0415

    collector = truth_collector or _default_truth_collector
    vt_dir = results_dir / "vendor_truth" / spec.name
    bf_dir = results_dir / "batfish" / spec.name
    hh_dir = results_dir / "hammerhead" / spec.name
    diff_dir = results_dir / "diff_frr_only_truth" / spec.name

    result = FrrOnlyTruthResult(
        topology=spec.name,
        status="failed",
        started_iso=started,
        finished_iso=started,
        truth_source="containerlab-frr",
    )

    try:
        # Rendering has to happen before truth collection so the truth
        # collector has configs to push onto the containers.
        log.info("[%s] rendering configs (frr-only-truth)", spec.name)
        render_topology(spec, workdir)
        configs_dir = workdir / "configs"

        log.info("[%s] collecting containerlab vendor truth", spec.name)
        t_truth_0 = _time.monotonic()
        collector(spec, workdir, results_dir)
        truth_wall = _time.monotonic() - t_truth_0
        result.vendor_truth_path = vt_dir

        # Run Batfish + Hammerhead against the same configs_dir.
        bf_wall: float | None = None
        hh_wall: float | None = None
        if hooks.batfish is not None:
            bf_dir.mkdir(parents=True, exist_ok=True)
            log.info("[%s] running batfish (frr-only-truth)", spec.name)
            t0 = _time.monotonic()
            hooks.batfish(configs_dir, bf_dir, spec.name)
            bf_wall = _time.monotonic() - t0
            result.batfish_path = bf_dir
        if hooks.hammerhead is not None:
            hh_dir.mkdir(parents=True, exist_ok=True)
            log.info("[%s] running hammerhead (frr-only-truth)", spec.name)
            t0 = _time.monotonic()
            hooks.hammerhead(configs_dir, hh_dir, spec.name)
            hh_wall = _time.monotonic() - t0
            result.hammerhead_path = hh_dir

        bf_sim = _read_stat(bf_dir / "batfish_stats.json", "simulate_s")
        bf_total_stat = _read_stat(bf_dir / "batfish_stats.json", "total_s")
        _assert_simulate_le_total("batfish", spec.name, bf_sim, bf_total_stat)
        hh_sim = _read_stat(hh_dir / "hammerhead_stats.json", "simulate_s")
        hh_rib = _read_stat(hh_dir / "hammerhead_stats.json", "rib_total_s")
        hh_total_stat = _read_stat(hh_dir / "hammerhead_stats.json", "total_s")
        _assert_simulate_le_total("hammerhead", spec.name, hh_sim, hh_total_stat)
        if hh_sim is not None and hh_rib is not None:
            _assert_simulate_le_total(
                "hammerhead (simulate+rib)",
                spec.name,
                hh_sim + hh_rib,
                hh_total_stat,
            )
        hh_sim_plus_rib = (
            (hh_sim or 0.0) + (hh_rib or 0.0)
            if hh_sim is not None or hh_rib is not None
            else None
        )

        agreement = _compute_three_way_agreement(
            spec=spec,
            results_dir=results_dir,
            diff_dir=diff_dir,
            filter_loopback_host=hooks.filter_loopback_host,
            truth_wall_s=truth_wall,
            batfish_wall_s=bf_wall,
            hammerhead_wall_s=hh_wall,
            batfish_simulate_s=bf_sim,
            hammerhead_simulate_s=hh_sim,
            hammerhead_rib_total_s=hh_rib,
            hammerhead_simulate_plus_rib_s=hh_sim_plus_rib,
        )
        result.three_way_agreement = agreement
        result.diff_path = diff_dir
        result.status = "passed"
    except Exception as exc:  # noqa: BLE001 — pipeline is the top-level catch-all
        result.error = f"{type(exc).__name__}: {exc}"
        log.error("[%s] frr-only-truth failed: %s", spec.name, result.error)
    finally:
        result.finished_iso = _now_iso()
    return result


def _compute_three_way_agreement(
    *,
    spec: TopologySpec,
    results_dir: Path,
    diff_dir: Path,
    filter_loopback_host: bool,
    truth_wall_s: float | None,
    batfish_wall_s: float | None,
    hammerhead_wall_s: float | None,
    batfish_simulate_s: float | None,
    hammerhead_simulate_s: float | None,
    hammerhead_rib_total_s: float | None = None,
    hammerhead_simulate_plus_rib_s: float | None = None,
) -> ThreeWayAgreement:
    """Diff all three pairs, persist records + agreement.json.

    Pure index-pair diffs; re-uses the sim-only index helper for consistency
    with the head-to-head path. Writes ``agreement.json`` + ``records.json``
    under ``diff_dir`` so the report renderer has a stable artifact location.
    """
    import json  # noqa: PLC0415

    workspace = load_fib_workspace(results_dir, spec.name)
    truth_ix = _sim_only_index(workspace.vendor, filter_loopback_host)
    batfish_ix = _sim_only_index(workspace.batfish, filter_loopback_host)
    hammer_ix = _sim_only_index(workspace.hammerhead, filter_loopback_host)

    bt = _pairwise_agreement(batfish_ix, truth_ix)
    ht = _pairwise_agreement(hammer_ix, truth_ix)
    bh = _pairwise_agreement(batfish_ix, hammer_ix)

    agreement = ThreeWayAgreement(
        topology=spec.name,
        truth_source="containerlab-frr",
        truth_routes=len(truth_ix),
        batfish_routes=len(batfish_ix),
        hammerhead_routes=len(hammer_ix),
        truth_simulate_s=truth_wall_s,
        batfish_wall_s=batfish_wall_s,
        hammerhead_wall_s=hammerhead_wall_s,
        batfish_simulate_s=batfish_simulate_s,
        hammerhead_simulate_s=hammerhead_simulate_s,
        hammerhead_rib_total_s=hammerhead_rib_total_s,
        hammerhead_simulate_plus_rib_s=hammerhead_simulate_plus_rib_s,
        batfish_vs_truth_both_keys=bt["both"],
        batfish_vs_truth_union_keys=bt["union"],
        batfish_vs_truth_presence=bt["presence"],
        batfish_vs_truth_next_hop=bt["next_hop"],
        batfish_vs_truth_protocol=bt["protocol"],
        batfish_vs_truth_bgp_attr=bt["bgp_attr"],
        hammerhead_vs_truth_both_keys=ht["both"],
        hammerhead_vs_truth_union_keys=ht["union"],
        hammerhead_vs_truth_presence=ht["presence"],
        hammerhead_vs_truth_next_hop=ht["next_hop"],
        hammerhead_vs_truth_protocol=ht["protocol"],
        hammerhead_vs_truth_bgp_attr=ht["bgp_attr"],
        batfish_vs_hammerhead_both_keys=bh["both"],
        batfish_vs_hammerhead_union_keys=bh["union"],
        batfish_vs_hammerhead_presence=bh["presence"],
        batfish_vs_hammerhead_next_hop=bh["next_hop"],
        batfish_vs_hammerhead_protocol=bh["protocol"],
        batfish_vs_hammerhead_bgp_attr=bh["bgp_attr"],
    )

    diff_dir.mkdir(parents=True, exist_ok=True)
    (diff_dir / "agreement.json").write_text(json.dumps(agreement.as_dict(), indent=2) + "\n")
    return agreement


def _pairwise_agreement(ix_x: dict, ix_y: dict) -> dict[str, float | int]:
    """Compute agreement counts for two pre-indexed FIB dicts.

    Returns a dict with keys ``both`` (int), ``union`` (int), ``presence``
    (Jaccard, float), ``next_hop`` / ``protocol`` / ``bgp_attr`` (float,
    1.0 on empty intersection to match the sim-only convention).
    """
    union_keys = set(ix_x) | set(ix_y)
    both_keys = set(ix_x) & set(ix_y)
    nh_agree = 0
    proto_agree = 0
    bgp_total = 0
    bgp_agree = 0
    for key in both_keys:
        x = ix_x[key]
        y = ix_y[key]
        if _nh_sets_equal_sim_only(x, y):
            nh_agree += 1
        if x.protocol == y.protocol:
            proto_agree += 1
        if x.protocol == "bgp" and y.protocol == "bgp":
            bgp_total += 1
            if (
                _as_path_equal_sim_only(x.as_path, y.as_path)
                and x.local_pref == y.local_pref
                and x.med == y.med
            ):
                bgp_agree += 1
    denom_both = len(both_keys) or 1
    denom_union = len(union_keys) or 1
    return {
        "both": len(both_keys),
        "union": len(union_keys),
        "presence": len(both_keys) / denom_union if union_keys else 1.0,
        "next_hop": nh_agree / denom_both if both_keys else 1.0,
        "protocol": proto_agree / denom_both if both_keys else 1.0,
        "bgp_attr": bgp_agree / bgp_total if bgp_total else 1.0,
    }


def aggregate_sim_only(per_topology: list[SimOnlyAgreement]) -> dict:
    """Bench summary with both naive and coverage-honest agreement means.

    Two reductions are emitted side-by-side:

    * ``*_agreement_mean`` is the arithmetic mean over *all* topologies,
      treating a zero-intersection topology (e.g. Batfish parser failure
      \u2192 0 routes) as 1.0 \u2014 a vacuous truth that flatters the
      headline. Kept for backward-compatibility with existing
      ``results/bench_summary.json`` consumers.

    * ``*_agreement_mean_covered`` is the mean over only those
      topologies where ``both_sides_keys > 0``. This is the honest
      reduction: it excludes topologies where no meaningful comparison
      could happen. Reviewers should quote this number alongside the
      ``covered_topology_count`` so the denominator is explicit.

    ``mean_coverage`` is the arithmetic mean of per-topology Jaccard
    coverage; a low value flags how much of the summary is running on
    empty intersections.
    """
    n = len(per_topology)
    if n == 0:
        return {"topology_count": 0}

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 1.0

    covered = [a for a in per_topology if a.both_sides_keys > 0]

    # Trial count is uniform across topologies within a single bench run
    # (CLI passes --trials N to every topology). Surface it at the summary
    # level so the report can key off a single `trials` number.
    trial_counts = {
        (a.trials or {}).get("n", 1) for a in per_topology
    }
    trials_n = (
        trial_counts.pop() if len(trial_counts) == 1 else max(trial_counts, default=1)
    )

    return {
        "topology_count": n,
        "covered_topology_count": len(covered),
        "trials": trials_n,
        "mean_coverage": _mean([a.coverage for a in per_topology]),
        "next_hop_agreement_mean": _mean([a.next_hop_agreement for a in per_topology]),
        "protocol_agreement_mean": _mean([a.protocol_agreement for a in per_topology]),
        "bgp_attr_agreement_mean": _mean([a.bgp_attr_agreement for a in per_topology]),
        "next_hop_agreement_mean_covered": _mean(
            [a.next_hop_agreement for a in covered]
        ),
        "protocol_agreement_mean_covered": _mean(
            [a.protocol_agreement for a in covered]
        ),
        "bgp_attr_agreement_mean_covered": _mean(
            [a.bgp_attr_agreement for a in covered]
        ),
        "total_batfish_routes": sum(a.batfish_routes for a in per_topology),
        "total_hammerhead_routes": sum(a.hammerhead_routes for a in per_topology),
        "total_batfish_wall_s": sum(a.batfish_wall_s or 0.0 for a in per_topology),
        "total_hammerhead_wall_s": sum(a.hammerhead_wall_s or 0.0 for a in per_topology),
        "topology_details": [a.as_dict() for a in per_topology],
    }
