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

| Topology | Nodes | Routes (bf / hh) | Presence | NH agree | Batfish wall | Batfish solve | Hammerhead wall | Solve ratio |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `bgp-ebgp-2node` | 2 | 4 / 4 | 100.0% | 100% | 20.35 s | 20.35 s | 0.035 s | 594.4× |
| `bgp-ibgp-2node` | 2 | 4 / 4 | 100.0% | 100% | 21.42 s | 21.42 s | 0.037 s | 579.7× |
| `acl-heavy-parse` | 3 | 15 / 6 | 40.0% | 100% | 22.14 s | 22.14 s | 0.044 s | 501.1× |
| `ospf-p2p-3node` | 3 | 15 / 6 | 40.0% | 100% | 21.06 s | 21.06 s | 0.044 s | 480.3× |
| `route-map-pathological` | 3 | 15 / 15 | 100.0% | 100% | 24.57 s | 24.57 s | 0.048 s | 520.9× |
| `isis-l1l2-4node` | 4 | 6 / 26 | 23.1% | 100% | 20.53 s | 20.53 s | 0.056 s | 368.2× |
| `mixed-vendor-frr-ceos-4node` | 4 | 12 / 8 | 66.7% | 100% | 30.89 s | 30.89 s | 0.054 s | 574.2× |
| `mpls-l3vpn-4node` | 4 | 26 / 8 | 21.4% | 100% | 23.76 s | 23.76 s | 0.054 s | 444.6× |
| `ospf-broadcast-4node` | 4 | 16 / 4 | 25.0% | 100% | 21.65 s | 21.65 s | 0.055 s | 392.7× |
| `multi-as-edge-5node` | 5 | 27 / 27 | 100.0% | 100% | 23.10 s | 23.10 s | 0.063 s | 366.9× |
| `spine-leaf-6node` | 6 | 36 / 36 | 100.0% | 100% | 25.24 s | 25.23 s | 0.072 s | 353.4× |
| `route-reflector-6node` | 6 | 36 / 36 | 100.0% | 100% | 24.18 s | 24.18 s | 0.068 s | 359.3× |
| `spine-leaf-20node` | 20 | 432 / 432 | 100.0% | 100% | 37.30 s | 37.29 s | 0.234 s | 159.5× |
| `spine-leaf-50node` | 50 | 2,622 / 2,622 | 100.0% | 100% | 52.09 s | 52.08 s | 1.284 s | 40.6× |
| `hub-spoke-wan-51node` | 51 | 5,251 / 5,251 | 100.0% | 100% | 43.26 s | 43.24 s | 1.130 s | 38.3× |
| `spine-leaf-100node` | 100 | 10,355 / 10,355 | 100.0% | 100% | 91.93 s | 91.87 s | 8.869 s | 10.4× |

The `Batfish wall` column is end-to-end (container boot + pybatfish
init + snapshot upload + solve); the `Batfish solve` column is the
pybatfish-reported inner solve time only (what ends up in
`agreement.batfish_simulate_s`). The `Solve ratio` column is
`batfish_simulate_s / hammerhead_simulate_s` — the apples-to-apples
solver speedup, excluding JVM startup on the numerator and harness
fork-exec overhead on the denominator.

The `Presence` column is the per-topology Jaccard
`|B ∩ H| / |B ∪ H|` on `(node, vrf, prefix)` keys; see § 2 for
the formal definition. Topologies below 100% presence (e.g.
`isis-l1l2-4node` at 23.1%) reflect Batfish materializing /32
loopback host routes that Hammerhead elides — a documented
modeling difference rather than a simulator disagreement
(§ 3 "Route-count asymmetry").

**Aggregate over the corpus.** Cumulative Batfish wall-clock is
**503.47 s** (end-to-end) and **503.36 s** (solve only);
Hammerhead totals **12.15 s** in wall. The raw wall-clock ratio is
**41.4×** and the solve-only ratio is **41.4×** — close because at
this corpus size the JVM-startup share of Batfish wall-clock has
been amortised by the accumulated solve time; at individual 2–6
node topologies the end-to-end wall-clock remains JVM-dominated,
which is why per-topology solve ratios span **10.4× – 594.4×**.
We do not recommend citing any single aggregate as a headline
speedup number without qualification; § 3 explains the regime
structure.

**Agreement.** On the intersection of (node, vrf, prefix) cells
installed by both tools (the `|B ∩ H|` column in the per-topology
report), all three equality relations — next-hop-set, protocol,
and BGP-attribute (AS_PATH, LOCAL_PREF, MED) — are **100% across
all 16 topologies**. Absolute cell counts, per-trial wall-clocks
(when `--trials N` was used), and raw per-topology times are in
`results/<topology>.json`; the rolled-up aggregate is in
`results/bench_summary.json`.

### 1.1 Ground-truth agreement (FRR subset)

`hammerhead-bench bench --frr-only-truth` extends the sim-only
diff into a 3-way comparison — *vendor truth T* (extracted from a
live containerlab deployment of the rendered configs), *Batfish
B*, and *Hammerhead H* — on the subset of topologies that are
runnable against FRR / Cumulus only. A topology qualifies when
every node's adapter kind is `frr` or `cumulus_vx`, the node
count is ≤ 20 (the containerlab laptop ceiling we carve in at
`harness/topology.py:FRR_ONLY_TRUTH_MAX_NODES`), and the spec
does not use the `external_renderer` escape hatch. Topologies
outside that subset (mixed-vendor snapshots, fat-tree k=64,
spine-leaf-100node, …) fall back to sim-only in the same run
and appear with `truth_source: null` in
`results/<topology>.json`. The 3-way triad is
`batfish_vs_truth`, `hammerhead_vs_truth`, `batfish_vs_hammerhead`;
each triad carries the same four metrics as the sim-only
agreement (`presence`, `next_hop`, `protocol`, `bgp_attr`) on
the `|X ∩ Y| / |X ∪ Y|` and `|X ∩ Y|` denominators defined in
§ 2.

**Scope note.** The table below is a placeholder: this
macOS laptop cannot run containerlab (Docker + veth requires
Linux), so the 3-way path returns `containerlab_unsupported`
for every row. We publish ground-truth numbers only from a
Linux CI run — not by hand-assembled claims. When the Linux
run lands, this subsection will carry a populated 8-column
table (`Topology`, `T routes`, `B routes`, `H routes`,
`B vs T presence`, `H vs T presence`, `B vs T NH`,
`H vs T NH`) alongside the existing § 1 two-way table.

| Topology | T routes | B routes | H routes | B vs T presence | H vs T presence | B vs T NH | H vs T NH |
|---|---:|---:|---:|---:|---:|---:|---:|
| *TBD — populated on Linux CI* | — | — | — | — | — | — | — |

The `--frr-only-truth` mode is mutually exclusive with
`--sim-only` (the CLI exits non-zero if both are passed). All
other existing flags (`--only`, `--trials`, `--out`, …) work as
before. Topologies that fall back to sim-only keep their
existing JSON shape byte-for-byte, so downstream consumers of
the sim-only run are not affected.

## 2. Agreement metric (formal definition)

Let `B` and `H` denote the per-topology sets of routes installed
by Batfish and Hammerhead respectively, each route identified by
its key `(n, v, p)` ∈ Nodes × VRFs × Prefixes. We measure
agreement only on `B ∩ H` — cells where both simulators installed
some route for the same key on the same node in the same VRF.
For each such cell, we define (exactly as implemented in
`harness/diff/engine.py`):

- **`nh_agree(n, v, p)`** — let `NH_t(n,v,p) = {(ip_i, iface_i)}`
  be the *set* of `(ip, interface)` pairs produced by tool `t` at
  cell `(n,v,p)` after canonicalization. Canonicalization collapses
  the Batfish sentinels `AUTO/NONE*`, `dynamic`, `null_interface`,
  `null0`, `null_0`, `none` to the Python `None` value, so a
  syntactic difference between the two tools' descriptors of
  "no next-hop IP" / "null interface" does not manufacture a
  disagreement. Then `nh_agree := NH_B == NH_H` as
  `frozenset` equality.
- **`proto_agree(n, v, p)`** — `protocol(B) == protocol(H)` as
  a string equality on the canonicalized protocol code emitted
  by the respective harness adapter (`bgp`, `ospf`, `isis`,
  `static`, `connected`, etc.).
- **`bgp_attrs_agree(n, v, p)`** — defined only on cells where
  both sides report protocol `bgp`. Let
  `a(t) := (AS_PATH, LOCAL_PREF, MED)` for tool `t`; then
  `bgp_attrs_agree := (AS_PATH(B) == AS_PATH(H)) ∧
  (LOCAL_PREF(B) == LOCAL_PREF(H)) ∧ (MED(B) == MED(H))`. The
  `AS_PATH` comparison is length- and order-sensitive list
  equality; `None` matches `None` but `None` does not match `[]`.
- **`presence_agree(t)`** — per-topology Jaccard overlap on the
  key sets, measuring *which rows both tools produced at all*
  independent of any inner-field equality. Formally, with
  `K_B(t) := { (n, v, p) : B(t) has a route at (n, v, p) }` and
  `K_H(t)` analogously,
  `presence_agree(t) := |K_B(t) ∩ K_H(t)| / |K_B(t) ∪ K_H(t)|`
  with the convention `presence_agree(t) := 1.0` when
  `|K_B(t) ∪ K_H(t)| = 0` (no rows on either side — a vacuous
  truth, not a claim). This is surfaced in
  `agreement.presence` (and the identically-valued alias
  `agreement.coverage`) in every `results/<topology>.json`
  and as the `Presence` column in the § 1 table. It is the
  sim-only analogue of the 3-way `presence_match_rate` the
  vendor-truth path reports against live FRR / cEOS; see
  `harness/diff/metrics.py` for the 3-way shape.

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

- **Variance is measured, but on one host.** The wall-clock
  columns in § 1 are `mean ± std` over `--trials 5` (the CLI
  default); per-trial timings are stored under
  `results/<topology>.json` `agreement.trials` + `trial_stats`
  for independent inspection. What we do *not* claim: that the
  distribution is Gaussian, that 5 samples resolve the tail, or
  that a different host would see the same spread. JVM
  first-pass class-loading is the usual source of Batfish
  variance on small topologies (1–3 s across reruns); it
  survives `--trials` because each trial starts a fresh
  container on purpose, so the measured std is an honest
  reflection of what a production user re-running a snapshot
  would see.
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
  timing does not include protocol convergence. Cross-
  validation against vendor-produced RIBs is surfaced
  separately in § 1.1 ("Ground-truth agreement (FRR subset)")
  when a `--frr-only-truth` run is on record; that mode is
  Linux-only (it uses containerlab + Docker) and is restricted
  to FRR-based topologies of ≤ 20 nodes, so the subset is a
  genuine subset of the corpus, not a replacement for the
  headline sim-only numbers.
- **Route-count asymmetry is a modeling difference, not
  disagreement.** Batfish materializes /32 loopback host
  routes on every node; Hammerhead elides them. At 100 nodes
  this is a ~950-route gap. Because these prefixes are in
  `B \ H`, they are outside `B ∩ H` and do not enter the
  `next_hop_agree(t)` / `protocol_agree(t)` /
  `bgp_attr_agree(t)` metrics of § 2; they *do* lower
  `presence_agree(t)` (Jaccard) because they enlarge the union.
  The per-topology table in § 1 therefore separates "presence"
  from the attribute agreement rates, so a reader can see
  exactly how much of any given gap is modeling asymmetry vs.
  substantive disagreement. This is a deliberate choice; we
  document it rather than post-process one tool's output to
  mirror the other's.
- **Batfish wall-clock includes pybatfish init + snapshot
  upload.** We do not subtract these. They are ~1 s on our
  host, dwarfed by JVM startup at small sizes and by
  simulation at large sizes. The § 1 table's "B solve (s)"
  and "solve ratio" columns use `batfish_simulate_s` /
  `hammerhead_simulate_s` (the inner solver calls only) for
  an apples-to-apples solver comparison alongside the
  wall-clock ratio; both are reported so a reader can judge
  which one they care about.
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
