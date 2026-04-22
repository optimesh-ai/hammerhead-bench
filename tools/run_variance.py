"""Run the sim-only bench N times and report mean / stddev on headline metrics.

Usage:

    HAMMERHEAD_CLI=/path/to/hammerhead python3 tools/run_variance.py --runs 5

Writes:
    results/variance/run_<i>.json        one bench_summary per run
    results/variance/variance_summary.json
    results/variance/variance_summary.md

Reported metrics (mean ± stddev across runs):
    - next_hop_agreement_mean
    - protocol_agreement_mean
    - bgp_attr_agreement_mean
    - next_hop_agreement_mean_covered
    - protocol_agreement_mean_covered
    - bgp_attr_agreement_mean_covered
    - mean_coverage
    - total_batfish_wall_s
    - total_hammerhead_wall_s
    - hammerhead_speedup = total_batfish_wall_s / total_hammerhead_wall_s

Per-topology wall times (hammerhead + batfish) also captured so we can
publish min/median/max tables.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results"
VARIANCE = RESULTS / "variance"


def _mean_stddev(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return (0.0, 0.0)
    if len(xs) == 1:
        return (float(xs[0]), 0.0)
    return (statistics.fmean(xs), statistics.stdev(xs))


def _fmt(m: float, s: float, pct: bool = False) -> str:
    if pct:
        return f"{m*100:.2f}% ± {s*100:.2f}pp"
    return f"{m:.4f} ± {s:.4f}"


def run_once(i: int) -> dict:
    print(f"[variance] run {i} ...", flush=True)
    env = os.environ.copy()
    # preserve HAMMERHEAD_CLI from caller env
    proc = subprocess.run(
        [
            str(REPO / ".venv" / "bin" / "hammerhead-bench"),
            "bench",
            "--sim-only",
            # fat-tree-k64 is a scale fixture (5,120 switches) that takes
            # Batfish orders of magnitude longer than the 16 real topologies.
            # Excluded here so variance reporting stays comparable across
            # repeatedly-run baseline topologies.
            "--skip",
            "fat-tree-k64",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-2000:])
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError(f"bench run {i} failed rc={proc.returncode}")

    src = RESULTS / "bench_summary.json"
    dst = VARIANCE / f"run_{i}.json"
    shutil.copyfile(src, dst)
    return json.loads(dst.read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    if "HAMMERHEAD_CLI" not in os.environ:
        sys.stderr.write("HAMMERHEAD_CLI must be set before invoking this script\n")
        return 2

    VARIANCE.mkdir(parents=True, exist_ok=True)

    summaries: list[dict] = []
    for i in range(1, args.runs + 1):
        summaries.append(run_once(i))

    keys = [
        "next_hop_agreement_mean",
        "protocol_agreement_mean",
        "bgp_attr_agreement_mean",
        "next_hop_agreement_mean_covered",
        "protocol_agreement_mean_covered",
        "bgp_attr_agreement_mean_covered",
        "mean_coverage",
        "total_batfish_wall_s",
        "total_hammerhead_wall_s",
    ]
    pct_keys = {
        "next_hop_agreement_mean",
        "protocol_agreement_mean",
        "bgp_attr_agreement_mean",
        "next_hop_agreement_mean_covered",
        "protocol_agreement_mean_covered",
        "bgp_attr_agreement_mean_covered",
        "mean_coverage",
    }

    agg: dict[str, dict[str, float]] = {}
    for k in keys:
        xs = [float(s.get(k, 0.0)) for s in summaries]
        m, sd = _mean_stddev(xs)
        agg[k] = {"mean": m, "stddev": sd, "values": xs}

    # derive speedup per run, then aggregate
    speedups = [
        (s["total_batfish_wall_s"] / s["total_hammerhead_wall_s"])
        if s["total_hammerhead_wall_s"] > 0 else math.nan
        for s in summaries
    ]
    m, sd = _mean_stddev(speedups)
    agg["hammerhead_speedup"] = {"mean": m, "stddev": sd, "values": speedups}

    # per-topology wall captures
    per_topology_walls: dict[str, dict[str, list[float]]] = {}
    for s in summaries:
        for t in s["topology_details"]:
            d = per_topology_walls.setdefault(
                t["topology"], {"batfish_wall_s": [], "hammerhead_wall_s": []}
            )
            d["batfish_wall_s"].append(float(t["batfish_wall_s"]))
            d["hammerhead_wall_s"].append(float(t["hammerhead_wall_s"]))

    per_topo_summary = {}
    for topo, walls in per_topology_walls.items():
        bm, bsd = _mean_stddev(walls["batfish_wall_s"])
        hm, hsd = _mean_stddev(walls["hammerhead_wall_s"])
        per_topo_summary[topo] = {
            "batfish_wall_s_mean": bm,
            "batfish_wall_s_stddev": bsd,
            "hammerhead_wall_s_mean": hm,
            "hammerhead_wall_s_stddev": hsd,
            "speedup_mean": (bm / hm) if hm > 0 else math.nan,
        }

    summary = {
        "runs": args.runs,
        "headline": agg,
        "per_topology": per_topo_summary,
    }

    (VARIANCE / "variance_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )

    # markdown report
    lines = []
    lines.append(f"# Sim-only variance — {args.runs} runs")
    lines.append("")
    lines.append("## Headline metrics (mean ± stddev)")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for k in keys:
        pair = agg[k]
        lines.append(f"| {k} | {_fmt(pair['mean'], pair['stddev'], pct=k in pct_keys)} |")
    pair = agg["hammerhead_speedup"]
    lines.append(f"| hammerhead_speedup | {pair['mean']:.2f}× ± {pair['stddev']:.2f} |")
    lines.append("")
    lines.append("## Per-topology wall time (mean ± stddev)")
    lines.append("")
    lines.append("| topology | batfish (s) | hammerhead (s) | speedup |")
    lines.append("|---|---:|---:|---:|")
    for topo in sorted(per_topo_summary):
        p = per_topo_summary[topo]
        lines.append(
            f"| {topo} | "
            f"{p['batfish_wall_s_mean']:.2f} ± {p['batfish_wall_s_stddev']:.2f} | "
            f"{p['hammerhead_wall_s_mean']:.3f} ± {p['hammerhead_wall_s_stddev']:.3f} | "
            f"{p['speedup_mean']:.1f}× |"
        )
    (VARIANCE / "variance_summary.md").write_text("\n".join(lines) + "\n")

    # stdout recap
    print()
    print("=== headline (mean ± stddev across runs) ===")
    for k in keys:
        pair = agg[k]
        print(f"  {k:45} {_fmt(pair['mean'], pair['stddev'], pct=k in pct_keys)}")
    pair = agg["hammerhead_speedup"]
    print(f"  {'hammerhead_speedup':45} {pair['mean']:.2f}× ± {pair['stddev']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
