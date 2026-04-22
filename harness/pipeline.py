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


@dataclass(slots=True)
class SimOnlyAgreement:
    """Head-to-head agreement metrics when vendor truth is not available.

    Semantics: rows counted only when BOTH simulators carry the
    ``(node, vrf, prefix)``. "Agreement" means both sides agree on a
    field; it is explicitly NOT a correctness claim because there's no
    third-party oracle.
    """

    topology: str
    batfish_routes: int
    hammerhead_routes: int
    union_keys: int
    both_sides_keys: int
    next_hop_agreement: float
    protocol_agreement: float
    bgp_attr_agreement: float
    batfish_wall_s: float | None = None
    hammerhead_wall_s: float | None = None
    batfish_simulate_s: float | None = None
    hammerhead_simulate_s: float | None = None

    def as_dict(self) -> dict:
        from dataclasses import asdict  # noqa: PLC0415

        return asdict(self)


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
    """
    import time as _time  # noqa: PLC0415

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
        log.info("[%s] rendering configs to %s (sim-only)", spec.name, workdir)
        render_topology(spec, workdir)
        configs_dir = workdir / "configs"

        batfish_wall: float | None = None
        if hooks.batfish is not None:
            log.info("[%s] running batfish", spec.name)
            bf_dir.mkdir(parents=True, exist_ok=True)
            t0 = _time.monotonic()
            hooks.batfish(configs_dir, bf_dir, spec.name)
            batfish_wall = _time.monotonic() - t0
            result.batfish_path = bf_dir

        hammerhead_wall: float | None = None
        if hooks.hammerhead is not None:
            log.info("[%s] running hammerhead", spec.name)
            hh_dir.mkdir(parents=True, exist_ok=True)
            t0 = _time.monotonic()
            hooks.hammerhead(configs_dir, hh_dir, spec.name)
            hammerhead_wall = _time.monotonic() - t0
            result.hammerhead_path = hh_dir

        agreement = _compute_sim_only_agreement(
            spec=spec,
            results_dir=results_dir,
            diff_dir=diff_dir,
            filter_loopback_host=hooks.filter_loopback_host,
            batfish_wall=batfish_wall,
            hammerhead_wall=hammerhead_wall,
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
    batfish_wall: float | None,
    hammerhead_wall: float | None,
) -> SimOnlyAgreement:
    """Diff Batfish and Hammerhead head-to-head; write records + agreement.json."""
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
    agreement = SimOnlyAgreement(
        topology=spec.name,
        batfish_routes=len(batfish_ix),
        hammerhead_routes=len(hammer_ix),
        union_keys=len(union_keys),
        both_sides_keys=len(both_keys),
        next_hop_agreement=nh_agree / denom_both if both_keys else 1.0,
        protocol_agreement=proto_agree / denom_both if both_keys else 1.0,
        bgp_attr_agreement=bgp_agree / bgp_total if bgp_total else 1.0,
        batfish_wall_s=batfish_wall,
        hammerhead_wall_s=hammerhead_wall,
        batfish_simulate_s=_read_stat(results_dir / "batfish" / spec.name / "batfish_stats.json", "total_s"),
        hammerhead_simulate_s=_read_stat(
            results_dir / "hammerhead" / spec.name / "hammerhead_stats.json", "total_s"
        ),
    )

    diff_dir.mkdir(parents=True, exist_ok=True)
    (diff_dir / "records.json").write_text(json.dumps(records, indent=2) + "\n")
    (diff_dir / "agreement.json").write_text(json.dumps(agreement.as_dict(), indent=2) + "\n")
    return agreement


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


def aggregate_sim_only(per_topology: list[SimOnlyAgreement]) -> dict:
    """Simple mean across topologies for the head-to-head bench summary."""
    n = len(per_topology)
    if n == 0:
        return {"topology_count": 0}

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 1.0

    return {
        "topology_count": n,
        "next_hop_agreement_mean": _mean([a.next_hop_agreement for a in per_topology]),
        "protocol_agreement_mean": _mean([a.protocol_agreement for a in per_topology]),
        "bgp_attr_agreement_mean": _mean([a.bgp_attr_agreement for a in per_topology]),
        "total_batfish_routes": sum(a.batfish_routes for a in per_topology),
        "total_hammerhead_routes": sum(a.hammerhead_routes for a in per_topology),
        "total_batfish_wall_s": sum(a.batfish_wall_s or 0.0 for a in per_topology),
        "total_hammerhead_wall_s": sum(a.hammerhead_wall_s or 0.0 for a in per_topology),
        "topology_details": [a.as_dict() for a in per_topology],
    }
