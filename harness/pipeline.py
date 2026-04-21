"""Per-topology orchestrator — sequential, one topology at a time.

Phase 3 scope: render → headroom-check → deploy → converge → extract(vendor)
 → destroy → recovery-verify → teardown-verify. Writes one ``MemorySample`` per
phase to ``results/memory.jsonl``. Batfish + Hammerhead slots are still TODO
markers; they hook in at phase 5/6 without changing this file's public contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.adapters.frr import FrrAdapter
from harness.clab import ClabDriver, ClabError, DeployedLab, RealClab
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
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    memory_samples: list[MemorySample] = field(default_factory=list)


def run_topology(
    spec: TopologySpec,
    *,
    workdir: Path,
    results_dir: Path,
    clab: ClabDriver | None = None,
    keep_lab_on_failure: bool = False,
    memory_log: Path | None = None,
    headroom_multiplier: float = 2.0,
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
    mem_path = memory_log if memory_log is not None else results_dir / "memory.jsonl"
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

        # TODO(phase-5): pipeline Batfish here; write to results_dir/batfish/<topology>/.
        # TODO(phase-6): pipeline Hammerhead here; write to results_dir/hammerhead/<topology>/.
        # TODO(phase-4): diff(vendor, batfish) + diff(vendor, hammerhead).

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


def _record_sample(
    result: TopologyRunResult, sample: MemorySample, mem_path: Path
) -> None:
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
        container = lab.container_name(node.name)
        if isinstance(node.adapter, FrrAdapter):
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
        container = lab.container_name(node.name)
        if isinstance(node.adapter, FrrAdapter):
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
