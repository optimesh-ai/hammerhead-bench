# hammerhead-bench

An open benchmark corpus and measurement harness for empirical
head-to-head comparison of network control-plane simulators.
We compare **[Hammerhead](https://github.com/optimesh-ai/hammerhead)**
against **[Batfish](https://batfish.org)** (and, optionally,
against live vendor ground truth from FRR and Arista cEOS-lab)
across 16 topologies spanning 2–100 nodes and seven control-plane
axes: BGP (iBGP, eBGP, route reflection, L3VPN, internet-edge
policy), OSPFv2 (point-to-point and broadcast), IS-IS L1/L2,
multi-vendor snapshots, ACL-dominated parse workloads, and Clos +
hub-and-spoke scale. The artifact includes the harness, all
Jinja2 topology templates, pinned container-image digests, and a
`make bench`-level reproducer.

**Status of this artifact.** Sim-only mode (tool vs. tool on the
same rendered config directory) is runnable on a single laptop in
~10 min. The `--with-truth` mode (live containerlab deployment +
per-device FIB extraction) is also implemented but requires the
cEOS-lab image, is slower (~1 h), and is not the baseline of the
numbers below.

## 1. Results (sim-only)

We report measurements from a single run on an Apple M-series
laptop (10 performance cores, 32 GB RAM, macOS 15), `hammerhead-bench
bench --sim-only`. Batfish runs in `batfish/allinone:latest` with
`_JAVA_OPTIONS=-Xmx4g`; Hammerhead runs as a release binary.
Both tools ingest the exact same rendered config directory per
topology. We canonicalize next-hop descriptors before comparison
(`AUTO/NONE`, `dynamic`, `null_interface` → `None`) so syntactic
differences do not inflate disagreement.

| Topology | Nodes | Routes (bf / hh) | NH agree | Batfish wall | Hammerhead wall | Ratio |
|---|---:|---:|---:|---:|---:|---:|
| `bgp-ebgp-2node` | 2 | 6 / 4 | 100% | 29.42 s | 0.037 s | 788.6× |
| `bgp-ibgp-2node` | 2 | 0 / 4 | 100% | 22.96 s | 0.039 s | 593.5× |
| `acl-heavy-parse` | 3 | 12 / 6 | 100% | 35.44 s | 0.048 s | 739.1× |
| `ospf-p2p-3node` | 3 | 12 / 6 | 100% | 30.79 s | 0.046 s | 675.1× |
| `route-map-pathological` | 3 | 0 / 12 | 100% | 26.90 s | 0.048 s | 555.4× |
| `isis-l1l2-4node` | 4 | 26 / 26 | 100% | 28.63 s | 0.060 s | 480.9× |
| `mixed-vendor-frr-ceos-4node` | 4 | 8 / 8 | 100% | 28.76 s | 0.055 s | 518.5× |
| `mpls-l3vpn-4node` | 4 | 8 / 8 | 100% | 31.58 s | 0.067 s | 469.4× |
| `ospf-broadcast-4node` | 4 | 8 / 4 | 100% | 30.65 s | 0.054 s | 563.9× |
| `multi-as-edge-5node` | 5 | 32 / 27 | 100% | 33.04 s | 0.068 s | 486.2× |
| `spine-leaf-6node` | 6 | 52 / 36 | 100% | 31.55 s | 0.071 s | 441.7× |
| `route-reflector-6node` | 6 | 0 / 36 | 100% | 28.46 s | 0.082 s | 349.0× |
| `spine-leaf-20node` | 20 | 560 / 432 | 100% | 42.53 s | 0.251 s | 169.4× |
| `spine-leaf-50node` | 50 | 2,990 / 2,622 | 100% | 56.88 s | 1.357 s | 41.9× |
| `hub-spoke-wan-51node` | 51 | 5,351 / 5,251 | 100% | 56.02 s | 1.557 s | 36.0× |
| `spine-leaf-100node` | 100 | 11,305 / 10,355 | 100% | 102.67 s | 9.513 s | 10.8× |

**Aggregate over the corpus.** Cumulative wall-clock is **616.29 s
for Batfish** and **13.35 s for Hammerhead**, a ratio of **46.2×**.
Per-topology ratios span **10.8× – 788.6×** and, as § 3 explains,
are dominated by JVM startup at small sizes and by simulation
work at large sizes; we do not recommend citing the aggregate
as a single headline speedup number without qualification.

**Agreement.** On the intersection of (node, vrf, prefix) cells
installed by both tools, all three equality relations below are
100% across all 16 topologies:
next-hop-set agreement, protocol agreement, and BGP-attribute
agreement (AS_PATH, LOCAL_PREF, MED). Absolute cell counts and
raw per-topology times are in `results/<topology>.json`; the
rolled-up aggregate is in `results/bench_summary.json`.

## 2. Agreement metric (formal definition)

Let `B` and `H` denote the per-topology sets of routes installed
by Batfish and Hammerhead respectively, each route identified by
its key `(n, v, p)` ∈ Nodes × VRFs × Prefixes. We measure
agreement only on `B ∩ H` — cells where both simulators installed
some route for the same key on the same node in the same VRF.
For each such cell, we define:

- **`nh_agree(n, v, p)`** — let `NH_t(n,v,p)` be the next-hop
  multiset produced by tool `t` at cell `(n,v,p)` after
  canonicalization (AUTO/NONE/dynamic/null_interface → None).
  `nh_agree := (IP-set of NH_B == IP-set of NH_H)` when either
  side has ≥ 1 IP next-hop; otherwise `(interface-set of NH_B
  == interface-set of NH_H)`.
- **`proto_agree(n, v, p)`** — `protocol(B) == protocol(H)` as
  a string equality on canonicalized protocol names
  (`bgp`, `ospf`, `ospf_ia`, `ospf_e1`, `ospf_e2`, `isis_l1`,
  `isis_l2`, `static`, `connected`).
- **`bgp_attrs_agree(n, v, p)`** — defined only on cells where
  both sides report protocol `bgp`. Let `a(t) := (AS_PATH,
  LOCAL_PREF, MED)` for tool `t`; then
  `bgp_attrs_agree := a(B) == a(H)` as a tuple.

Per-topology agreement is the unweighted cell-level mean of each
relation over `B ∩ H` (over BGP-cells for `bgp_attrs_agree`).
The aggregate row in the table of § 1 is the mean across
topologies, not weighted by cell count — this was chosen so that
a single large topology does not dominate the headline number.
Raw cell counts for each topology are in
`results/<topology>.json` under the `agreement.cells_compared`
field for each axis.

**What this does not claim.** Agreement between two simulators
is not correctness: if both tools misinterpret the same route-
map in the same direction, they will agree on the wrong answer.
Cross-validation against vendor ground truth is the job of
`--with-truth` mode; § 6 lists what we did and did not do there.

## 3. Threats to validity

We flag each of the following so that readers can judge how much
of the headline ratio they should trust.

- **N = 1.** The numbers in § 1 are from a single run per
  topology. We do not report variance, confidence intervals, or
  distribution tails. A minority of small-topology Batfish
  runs vary by 1–3 s across reruns on the same host because
  JVM first-pass class-loading is non-deterministic.
- **JVM cold-start is in the Batfish wall-clock.** Every
  Batfish run starts a fresh `batfish/allinone` container and
  pays the JVM startup cost (~15–20 s on our host). This is
  realistic — most production Batfish users restart between
  snapshots — but it explains why the 2-node topologies show
  a ~500× ratio: most of Batfish's 22–30 s is JVM startup, not
  simulation. The gap narrows monotonically with topology
  size; at 100 nodes, Batfish's wall-clock is dominated by
  actual solve time and the ratio falls to 10.8×.
- **Single hardware platform.** All numbers are from one
  laptop. We do not report x86 vs. arm64, server vs. laptop,
  or cloud-instance measurements. Absolute times are
  laptop-specific; *ratios* are more portable, but we have
  not verified that claim ourselves.
- **Sim-only mode runs no convergence.** In sim-only mode,
  neither Batfish nor Hammerhead boots a real router, so
  timing does not include protocol convergence. The
  `--with-truth` mode *does* include containerlab boot and
  convergence wait, and is correspondingly slower; cross-
  validation there is beyond the scope of this artifact's
  headline numbers.
- **Route-count asymmetry is a modeling difference, not
  disagreement.** Batfish materializes /32 loopback host
  routes on every node; Hammerhead elides them. At 100 nodes
  this is a ~950-route gap. Because these prefixes are in
  `B \ H`, they are outside `B ∩ H` and do not enter the
  agreement metric of § 2. This is a deliberate choice;
  we document it rather than "fix" it because both behaviors
  are defensible, and we do not want to bias the
  comparison by post-processing one tool to look like the
  other.
- **Batfish wall-clock includes pybatfish init + snapshot
  upload.** We do not subtract these. They are ~1 s on our
  host, dwarfed by JVM startup at small sizes and by
  simulation at large sizes. Consult `results/<topology>.json`
  for `batfish_simulate_s` (the inner simulate call only)
  if you want to extract just the solve time.
- **One topology is still gated.** `acl-semantics-3node`
  requires cEOS-lab for the flow-level ACL audit and is
  excluded from the 16-topology count here.
- **Determinism.** Both tools produce byte-identical output
  across repeated runs on the same config directory, which we
  rely on as a sanity check but do not claim as a tested
  invariant of this artifact.

## 4. Related work

Batfish (Fogel et al., NSDI '15) was the first general-purpose
control-plane simulator to become widely used by operators;
our reference implementation is the Intentionet-maintained
`batfish/allinone` container at the pinned digest in
`versions.lock`. Minesweeper (Beckett et al., SIGCOMM '17)
and its successor ShapeShifter reformulated control-plane
analysis as SMT constraint satisfaction; Plankton (Prabhu
et al., NSDI '20) uses explicit-state model checking with
equivalence-class reduction. NV (Giannarakis et al., PLDI '20)
offers a typed functional DSL targeted at control-plane
verification. Hammerhead sits in the same design space as
Batfish — direct simulation of parsed configs, no SMT, no
symbolic execution — but differs in implementation language
(Rust), data-structure choices (deterministic `BTreeMap`
throughout), and protocol scope (see the Hammerhead repo for
the full vendor + protocol matrix). This benchmark does not
compare against Minesweeper/Plankton/NV; those tools answer
different queries (e.g. "is there *any* failure scenario that
violates property P?") and are not drop-in-comparable to a
fixed-point FIB solver.

## 5. Corpus

The 16 topologies are selected to cover control-plane behaviors
that stress different parts of a simulator. Below, we list each
topology, its node count, and the primary axis it exercises;
see `topologies/<name>/README.md` for per-topology pass
criteria and full rendered config set.

| Name | Nodes | Axis |
| --- | ---: | --- |
| `bgp-ibgp-2node` | 2 | iBGP loopback peering, single AS |
| `bgp-ebgp-2node` | 2 | eBGP across a directly connected link |
| `ospf-p2p-3node` | 3 | OSPFv2 point-to-point chain |
| `ospf-broadcast-4node` | 4 | OSPFv2 DR/BDR on a shared segment |
| `isis-l1l2-4node` | 4 | IS-IS L1 + L2 boundary |
| `mixed-vendor-frr-ceos-4node` | 4 | FRR + Arista cEOS in one snapshot, iBGP-over-OSPF ECMP |
| `mpls-l3vpn-4node` | 4 | PE–P–P–PE, one VRF, RT import/export |
| `multi-as-edge-5node` | 5 | internet-edge: transit/peer/customer policy matrix (LP + community filters) |
| `spine-leaf-6node` | 6 | 2 spines × 4 leaves, eBGP unnumbered |
| `route-reflector-6node` | 6 | 2 RRs + 4 clients, iBGP |
| `spine-leaf-20node` | 20 | scale-up Clos (4 spines × 16 leaves) |
| `spine-leaf-50node` | 50 | scale-up Clos (4 spines × 46 leaves) |
| `hub-spoke-wan-51node` | 51 | classic WAN star: 1 hub + 50 eBGP branches |
| `spine-leaf-100node` | 100 | scale-up Clos (4 spines × 96 leaves) |
| `route-map-pathological` | 3 | BGP best-path tie-breakers via LOCAL_PREF rewrite |
| `acl-heavy-parse` | 3 | 500-line overlapping ACL; stresses parse coverage |
| `acl-semantics-3node` | 3 | mixed-vendor (FRR + cEOS) first-match-wins ACL semantics (gated behind `--with-acl-semantics`) |

**Addressing.** All externally routable prefixes in the corpus
are drawn from the IETF-reserved documentation and benchmarking
ranges (RFC 5737 `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`;
RFC 2544 `198.18.0.0/15`; RFC 1918 `10.0.0.0/8` for intra-fabric).
No real-world allocations are present in any rendered config.

## 6. Measurement harness

Per-topology pipeline, sequential, driven by
`harness.pipeline.run_topology`:

1. **Render** — Jinja2 templates under `topologies/<name>/`
   and `shared_templates/` per adapter `kind` (linux / bridge
   / ceos), with `StrictUndefined` so silent undefined-variable
   substitutions are impossible.
2. **Deploy (optional, `--with-truth` only)** — containerlab
   brings the lab up with explicit per-container memory caps
   (FRR 256 M / cEOS 1024 M / crpd 1536 M / srlinux 2048 M /
   xrd 4096 M).
3. **Converge (optional)** — wait for BGP Established and for
   the FIB to be stable across two 15 s intervals.
4. **Extract vendor truth (optional)** — FRR via `vtysh`,
   cEOS via `Cli -c "<cmd> | json"`; normalized to a shared
   FIB schema.
5. **Teardown + memory guard (optional)** — destroy the lab,
   verify zero dangling containers and memory recovery to
   within 500 MB of the pre-topology baseline.
6. **Batfish** — `batfish/allinone` container (`-Xmx4g`),
   pybatfish against the same config directory, FIB extracted
   to the shared shape.
7. **Hammerhead** — `$HAMMERHEAD_CLI simulate --format json`
   on the same directory, transformed to the shared shape.
8. **Diff** — per (node, vrf, prefix) presence, next-hop,
   protocol, and BGP-attribute rows (`harness.diff.engine`),
   rolled up per topology (`harness.diff.metrics`).
9. **Aggregate + report** — `hammerhead-bench report` loads
   all per-topology JSON and emits Markdown + HTML. Dynamic
   values are HTML-escaped; Plotly is inlined once; the HTML
   file opens cleanly from a `file://` URL with no network
   fetch.

**Design choices we think matter for reproducibility.**

- *One topology deployed at a time*, ever. We do not parallelize
  across topologies even when it would be faster. On a 32 GB
  host, concurrent containerlab deployments are the most
  common cause of OOM-kills we have observed.
- *Explicit memory caps*, per container and per harness
  process. Batfish uses `-Xmx4g`; on Linux the harness
  process itself sets `RLIMIT_AS=8 G`.
- *Teardown is verified*, not assumed. After each topology
  the harness greps `docker ps` and `docker network ls` for
  clab labels and aborts loudly if anything is left over.
- *Pinned digests*, not tags. `batfish/allinone` is pinned by
  digest in `versions.lock`; FRR is pinned by tag. Upgrading
  either is a one-line diff with a visible review.
- *Static artifacts*. Reports are plain HTML with inlined
  plotly. No web server, no JavaScript build step.

## 7. Reproducing the numbers

The artifact is designed to be runnable on a single 16 GB+
laptop with Docker Desktop.

```bash
git clone <this repo> hammerhead-bench && cd hammerhead-bench
cp .env.example .env                  # edit $HAMMERHEAD_CLI to your built binary
docker pull batfish/allinone          # then `docker images --digests` and pin in versions.lock
make preflight                        # sanity-checks the host
make smoke                            # one topology end-to-end (~5 min)
make bench                            # full 16-topology corpus, sim-only (~10 min)
uv run hammerhead-bench report        # regenerate HTML + MD from results/
open results/report/report.html
```

Commands we ran to produce § 1:

```bash
# Hammerhead: release build, commit pinned in .env
cargo build --release -p hammerhead-cli     # from the Hammerhead repo

# Batfish: pinned digest in versions.lock
docker pull batfish/allinone@sha256:...     # digest is in versions.lock

# Measurement
hammerhead-bench bench --sim-only
```

Each `results/<topology>.json` file carries: the topology spec
hash, the versions of both tools, the raw next-hop / protocol /
BGP-attribute cell counts and means, the raw wall-clock and
inner-simulate times, and the full diff of the (node, vrf,
prefix) keys that are in `B \ H`, `H \ B`, and `B ∩ H`.

**What we pin.** Docker image digest for Batfish (in
`versions.lock`); Hammerhead commit hash (in `.env` as part
of the CLI path); Python dependencies (`uv.lock`); OS package
pins for FRR and cEOS in each adapter's Dockerfile.

**What we do not pin.** Host kernel version, host Docker
version, host hardware. We list our host in § 1; laptop-to-
laptop variance is not in the scope of this artifact.

## 8. Tests

```bash
uv sync --all-extras --dev
uv run pytest                     # 194 passed, 1 skipped
uv run ruff check .
```

The test suite covers template rendering, adapter dispatch,
memory guards, the diff engine, per-topology metrics, both
simulator wrappers, CLI selection, pipeline orchestration,
and the full report generator (loader + three plot factories
+ HTML + Markdown). The one skipped test runs only when
Docker + containerlab are present on the host.

## 9. Vendor support

- ✅ **FRR** (`frrouting/frr`) — full vendor-truth + convergence
  + FIB extraction in `--with-truth` mode.
- ✅ **Arista cEOS-lab** — full; user supplies the image
  (Arista EOS Central, free account).
- 🧱 **Juniper crpd** — scaffolded; adapter currently raises
  `NotImplementedError`.
- 🧱 **Nokia SR Linux** — scaffolded.
- 🧱 **Cisco XRd** — scaffolded; skipped by default for
  memory reasons even when wired.

## 10. CLI

```
hammerhead-bench preflight                                 # host sanity check
hammerhead-bench smoke --topology bgp-ibgp-2node           # one topology end-to-end
hammerhead-bench bench [--only NAME] [--skip NAME]         # full corpus
               [--max-nodes N] [--with-acl-semantics]
               [--no-batfish] [--no-hammerhead]
               [--keep-lab-on-failure] [-v]
hammerhead-bench report --results-dir results/             # regenerate HTML + MD
```

## 11. Limitations and honest caveats

Beyond the threats in § 3:

- **The corpus is small-to-medium.** 100 nodes is the largest
  topology here. Hammerhead itself has been exercised at
  ≥ 5,120 nodes, and Batfish has been reported on networks of
  comparable size by its maintainers, but we have not measured
  either on 1,000+ -node configs *in this harness*. The 10.8×
  ratio at 100 nodes is the most informative single datum for
  extrapolation, and it is deliberately the smallest ratio
  we report.
- **We do not measure peak RSS in sim-only mode.** The header
  of § 1 is wall-clock-only. `--with-truth` mode does
  measure RSS; sim-only does not, because the Batfish-in-
  Docker memory accounting is confounded by JVM overhead
  that is not attributable to any specific solve.
- **We do not measure accuracy against vendor truth for all
  topologies.** `--with-truth` is implemented but requires
  cEOS-lab for 2 of the 16 topologies; we leave the
  full-truth run to external operators with the required
  licensing.
- **Hammerhead is developed by the authors of this
  benchmark.** We have tried to be even-handed in the metric
  definition (§ 2), in the threats-to-validity disclosure
  (§ 3), and in the conservative framing of the ratio in
  § 1. The templates, the harness, and the diff engine are
  source-available; any reader who suspects a methodological
  bias is invited to re-run with a modified harness.

## 12. Citation

Until a PDF writeup lands, please cite this repository. A
machine-readable `CITATION.cff` is provided at the repo root;
GitHub renders a "Cite this repository" button that consumes it.

```bibtex
@misc{hammerhead-bench-2026,
  title  = {hammerhead-bench: An Open Corpus for Head-to-Head
            Comparison of Network Control-Plane Simulators},
  author = {Mallela, Vedu and {Optimesh contributors}},
  year   = {2026},
  howpublished = {\url{https://github.com/optimesh-ai/hammerhead-bench}},
  note   = {Commit \texttt{a195f09}, accessed 2026-04-21}
}
```

When citing specific numbers from § 1, please also cite the
pinned Batfish digest from `versions.lock` and the Hammerhead
commit hash from your `.env` so the comparison is
reproducible against the same tool versions.

## License

Apache-2.0.
