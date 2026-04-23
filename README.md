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

| Topology | Nodes | Routes (bf / hh) | Presence | NH agree | BF wall | HH wall | Wall ratio | Fair ratio |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| bgp-ebgp-2node | 2 | 4 / 4 | 100% | 100.0% | 21.51±0.75s | 27.0±0.8ms | 795.8× | 198.5× |
| bgp-ibgp-2node | 2 | 4 / 4 | 100% | 100.0% | 22.07±0.94s | 27.2±0.8ms | 812.6× | 206.1× |
| acl-heavy-parse | 3 | 15 / 6 | 40% | 100.0% | 22.42±0.64s | 83.2±1.8ms | 269.5× | 217.0× |
| ospf-p2p-3node | 3 | 15 / 6 | 40% | 100.0% | 21.47±0.87s | 25.5±1.8ms | 841.3× | 219.4× |
| route-map-pathological | 3 | 15 / 15 | 100% | 100.0% | 22.75±0.51s | 28.1±0.9ms | 810.4× | 215.2× |
| isis-l1l2-4node | 4 | 6 / 26 | 23% | 100.0% | 21.88±0.66s | 28.0±0.6ms | 781.0× | 168.4× |
| mixed-vendor-frr-ceos-4node | 4 | 12 / 8 | 67% | 100.0% | 27.93±0.74s | 33.1±10.9ms | 843.7× | 187.4× |
| mpls-l3vpn-4node | 4 | 26 / 8 | 21% | 100.0% | 23.74±0.41s | 27.1±0.1ms | 876.2× | 253.2× |
| ospf-broadcast-4node | 4 | 16 / 4 | 25% | 100.0% | 22.72±0.65s | 26.4±0.9ms | 859.2× | 226.8× |
| multi-as-edge-5node | 5 | 27 / 27 | 100% | 100.0% | 24.45±0.74s | 27.7±0.8ms | 883.1× | 229.3× |
| route-reflector-6node | 6 | 36 / 36 | 100% | 100.0% | 25.57±1.38s | 29.3±0.5ms | 872.8× | 240.1× |
| spine-leaf-6node | 6 | 36 / 36 | 100% | 100.0% | 25.10±0.33s | 29.5±0.8ms | 850.3× | 235.5× |
| spine-leaf-20node | 20 | 432 / 432 | 100% | 100.0% | 35.65±0.67s | 51.0±6.3ms | 699.4× | 362.4× |
| spine-leaf-50node | 50 | 2622 / 2622 | 100% | 100.0% | 51.51±0.92s | 140.3±3.3ms | 367.2× | 299.7× |
| hub-spoke-wan-51node | 51 | 5251 / 5251 | 100% | 100.0% | 45.14±0.59s | 117.1±13.3ms | 385.5× | 259.2× |
| spine-leaf-100node | 100 | 10355 / 10355 | 100% | 100.0% | 90.26±1.00s | 577.9±17.7ms | 156.2× | 199.8× |

Wall ratio includes JVM startup and snapshot upload on the Batfish
side; fair ratio is the apples-to-apples solve+materialize comparison
defined in § 2.

The `Presence` column is the per-topology Jaccard
`|B ∩ H| / |B ∪ H|` on `(node, vrf, prefix)` keys; see § 2 for
the formal definition. Topologies below 100% presence (e.g.
`isis-l1l2-4node` at 23%) reflect Batfish materializing /32
loopback host routes that Hammerhead elides — a documented
modeling difference rather than a simulator disagreement
(§ 3 "Route-count asymmetry").

**Aggregate over the corpus.** `results/bench_summary.json`
reports three reductions of the per-topology `fair_ratio`
side-by-side (§ 2.4): `arithmetic_mean` (legacy baseline),
`geometric_mean` (multiplicative-scale central tendency — the one
reviewers should cite as "typical ratio"), and
`workload_weighted_mean` (route-count-weighted — pulls the headline
toward production-scale topologies and damps small-rig noise). On
the 16-topology corpus the arithmetic `fair_ratio` mean is **232×**
(median 223×, range **168×** at 4-node IS-IS to **362×** at 20-node
spine-leaf, n=16); the `geometric_mean` and `workload_weighted_mean`
columns in the summary JSON are what we cite when we need one
number. The corresponding arithmetic `wall_ratio` (includes Batfish
JVM + snapshot upload) is **694×**; post-migration (see § 3,
"Harness migration history") the `asym_ratio` lower bound collapses
onto `fair_ratio` because the Hammerhead-side RIB materialization
step is now inside the single `simulate --emit-rib all` call the
numerator measures. We do not recommend citing any single aggregate
as a headline speedup number without qualification; § 3 explains
the regime structure — ratios still contract with topology size on
the `wall_ratio` axis because Batfish's fixed ~22 s init cost
amortizes over larger solves, but the `fair_ratio` curve is now
much flatter than it was under the per-device-rib harness.

**Agreement.** On the intersection of (node, vrf, prefix) cells
installed by both tools (the `|B ∩ H|` column in the per-topology
report), all three equality relations — next-hop-set, protocol,
and BGP-attribute (AS_PATH, LOCAL_PREF, MED) — are **100% across
all 16 topologies**. Absolute cell counts, per-trial wall-clocks
(when `--trials N` was used), and raw per-topology times are in
`results/<topology>.json`; the rolled-up aggregate is in
`results/bench_summary.json`.

**One note on the 362× `fair_ratio` peak at `spine-leaf-20node`.**
20 nodes is where Batfish's solver crosses into a regime with
enough routes (432) that per-cell work starts to dominate its
fixed ~22 s JVM-warmup band, while Hammerhead is still inside its
own ~50 ms fixed-overhead floor (parse + build + single SPF). At
that crossover the numerator climbs faster than the denominator
and the ratio spikes. Beyond 20 nodes both tools scale roughly
linearly in route count and the `fair_ratio` settles into the
200-300× band — visible in the 50-/51-/100-node rows above.

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

### 2.1 Cell-level equality

Let `B` and `H` denote the per-topology sets of routes installed
by Batfish and Hammerhead respectively, each route identified by
its key `(n, v, p) ∈ Nodes × VRFs × Prefixes`. We measure cell-
level agreement only on `B ∩ H` — cells where both simulators
installed some route for the same key on the same node in the
same VRF. For each such cell, we define (exactly as implemented
in `harness/diff/engine.py`):

- **`nh_agree(n, v, p)`** — let `NH_t(n,v,p) = {(ip_i, iface_i)}`
  be the *set* of `(ip, interface)` pairs produced by tool `t` at
  cell `(n,v,p)` after canonicalization. Canonicalization collapses
  the Batfish sentinels `AUTO/NONE*`, `dynamic`, `null_interface`,
  `null0`, `null_0`, `none` to the Python `None` value, so a
  syntactic difference between the two tools' descriptors of
  "no next-hop IP" / "null interface" does not manufacture a
  disagreement. Then `nh_agree := NH_B == NH_H` as
  `frozenset` equality.
- **`proto_agree(n, v, p)`** — `protocol(B) == protocol(H)` as a
  string equality on the canonicalized protocol code emitted by
  the respective harness adapter (`bgp`, `ospf`, `isis`,
  `static`, `connected`, etc.).
- **`bgp_attrs_agree(n, v, p)`** — defined only on cells where
  both sides report protocol `bgp`. Let
  `a(t) := (AS_PATH, LOCAL_PREF, MED)` for tool `t`; then
  `bgp_attrs_agree := (AS_PATH(B) == AS_PATH(H)) ∧
  (LOCAL_PREF(B) == LOCAL_PREF(H)) ∧ (MED(B) == MED(H))`. The
  `AS_PATH` comparison is length- and order-sensitive list
  equality; `None` matches `None` but `None` does not match `[]`.

### 2.2 Presence Jaccard + Reference Canonicalizer

**Raw presence.** Per-topology Jaccard overlap on the key sets,
measuring *which rows both tools produced at all* independent of
any inner-field equality. With `K_B(t) := { (n, v, p) : B(t) has
a route at (n, v, p) }` and `K_H(t)` analogously,

```
presence_strict(t) := |K_B(t) ∩ K_H(t)| / |K_B(t) ∪ K_H(t)|
```

with `presence_strict(t) := 1.0` when `|K_B(t) ∪ K_H(t)| = 0`
(vacuous truth, not a claim). This is surfaced in every
`results/<topology>.json` as `agreement.presence_strict` +
`agreement.union_keys_strict` + `agreement.both_sides_keys_strict`.

**Reference Canonicalizer (symmetric /32 loopback reconciliation).**
FRR's zebra installs a `<loopback-ip>/32` host route for every
loopback interface, and Batfish additionally materializes the
IGP-advertised `/32` on every neighbour that sees the loopback
via IS-IS or OSPF. Hammerhead models loopbacks as prefix
originators rather than reinstalled destinations and elides those
`/32` entries. That is a modelling asymmetry, not a disagreement
on routing outcome, so comparing the two tools on `K_B` vs. `K_H`
directly penalises whichever one is more conservative about
re-installing its own loopbacks.

The Reference Canonicalizer is the symmetric post-process that
addresses the gap. Let `L(r)` be the predicate "route `r` is a
`/32` whose protocol is `connected` / `local` with a `lo*`
interface next-hop, OR whose protocol is `isis` / `ospf`". The
canonicalizer operates on the `K_B \ K_H` and `K_H \ K_B`
residues with three policies:

- **`LoopbackPolicy.STRIP`** — default. For every asymmetric
  `(n, v, p) ∈ (K_B △ K_H)` with `L` holding for the route that
  *does* appear, drop the key from *both* indexes. The diff is
  then computed on the symmetric residue, and presence / next-hop
  / protocol agreement never blame the tool that modelled the
  loopback more conservatively.
- **`LoopbackPolicy.MATERIALIZE`** — completionist view. For
  every asymmetric `(n, v, p)` with `L` holding, insert a copy on
  the missing side so the row enters `K_B ∩ K_H`. Informational
  only — the mirrored rows agree with themselves trivially, so
  next-hop agreement under this policy is not a correctness
  claim.
- **`LoopbackPolicy.PASSTHROUGH`** — no reconciliation, legacy
  behaviour preserved. Yields the Batfish-favouring upper bound
  on the presence gap.

The reconciled Jaccard is

```
presence(t) := |K_B'(t) ∩ K_H'(t)| / |K_B'(t) ∪ K_H'(t)|
```

where `K_X'(t)` is `K_X(t)` after the Reference Canonicalizer
ran under the selected policy. `presence(t) := 1.0` on empty
union, same vacuous-truth convention as raw presence. Every
per-topology JSON carries both `presence` and `presence_strict`
plus the integer count of rows the reconciler moved
(`loopback_reconciled_count`), so a reviewer can always audit
how much of the headline presence agreement came from
reconciliation vs. raw overlap.

### 2.3 Fair speedup ratio

**Infrastructure overhead vs. solver latency.** Batfish sits
behind a JVM + Jetty stack that takes ~18–22 s to reach REST
readiness on our host. The harness can run Batfish two ways:
cold (one container per trial, JVM cold-start paid every time)
or warm (one container per bench run via `BatfishService`, JVM
cold-start paid once). Both regimes are legitimate — cold mirrors
"operator re-snapshots a live network from scratch", warm mirrors
"operator re-queries the same snapshot in a running Batfish".
The sim-only path runs warm by default (`--persistent-batfish`,
toggled off with `--no-persistent-batfish`) and reports both the
first-trial `batfish_simulate_s_cold` and the mean over
subsequent warm trials (`batfish_simulate_s_warm_mean`) so the
reader can see the JVM cold-start tax separate from the
steady-state solve. `batfish_container_start_s` is the one-off
`docker run` + `wait_ready` cost attributed to the first trial
only.

**Fair ratio (headline).** With equivalent work on each side:

```
fair_ratio(t) := (batfish_query_routes_s + batfish_query_bgp_s)
               / (hammerhead_simulate_s  + hammerhead_rib_total_s)
```

- Numerator: the inner `InitRoutesQuestion` +
  `BgpRibQuestion` times reported by pybatfish. Excludes
  `docker run`, JVM / Jetty readiness, and snapshot upload
  (`batfish_init_snapshot_s`, reported separately on the Batfish
  sidecar so reviewers can see the architectural cost of the
  snapshot-upload step that Hammerhead doesn't have).
- Denominator: Hammerhead's inner solver (`simulate_s`) plus
  per-device RIB materialization (`rib_total_s`, zero in the
  post-b46eb45 bulk-emit migration because RIB is folded into
  `simulate --emit-rib all`).

Both numerator and denominator measure the analogous unit of
work — "compute the converged RIB for every (n, v, p) cell and
materialize it" — on the same two-phase pipeline shape. This is
the ratio reviewers should cite. When someone wants
"what speedup does an operator see end-to-end on a fresh
container?" the `wall_ratio` column beside it is the right
number; see below.

**Asym ratio (lower bound, retained for audit).** Pairing
Batfish's fused query+materialize against Hammerhead's solver
alone is Hammerhead-favouring — it charges Batfish for the
RIB-materialization step Hammerhead's numerator doesn't include.
We retain it as

```
asym_ratio(t) := (batfish_query_routes_s + batfish_query_bgp_s)
               /  hammerhead_simulate_s
```

solely so a reviewer can see how much of a headline gap is
solver performance vs. materialization-step shape. Every
`results/<topology>.json` carries an `asym_ratio_note` sibling
field stating verbatim that it is a lower bound. Post-b46eb45
the two ratios converge (`rib_total_s == 0` in the bulk-emit
path) so the caveat is a schema-stability breadcrumb rather
than an active warning.

**Wall ratio (upper bound, operator-facing).** The reverse-
direction aggregate

```
wall_ratio(t) := batfish_wall_s / hammerhead_wall_s
```

includes JVM cold-start + snapshot upload on the Batfish side
and the full harness fork-exec + subprocess overhead on the
Hammerhead side. On the persistent-service path `batfish_wall_s`
is the per-trial wall minus the amortised container start cost,
so `wall_ratio` under `--persistent-batfish` is closer to the
fair ratio than the `--no-persistent-batfish` version. Both are
surfaced in the JSON.

### 2.4 Corpus aggregates

Every `results/bench_summary.json` carries three reductions of
each ratio side-by-side — the reader picks the one their
question calls for:

- **`arithmetic_mean`** — `sum(ratios) / n`. Legacy baseline;
  sensitive to small-topology noise (a 2-node rig's 20 ms
  wall-time has ±50 % jitter that dominates a 16-sample mean).
- **`geometric_mean`** — `exp(mean(log(ratios)))`, computed in
  log space for numerical stability. Unbiased central tendency
  on a multiplicative scale, and the one reviewers should cite
  as "typical ratio" when the corpus spans multiple orders of
  magnitude (our wall-ratio range is 156× to 884×).
- **`workload_weighted_mean`** — `Σ w_i r_i / Σ w_i` with `w_i`
  equal to the Batfish-side route count. Pulls the headline
  toward production-scale topologies and damps small-rig noise.
  The right reduction for "what speedup does an operator see on
  a real fabric?".

The summary additionally surfaces `median`, `p25`, `p75`, `min`,
`max`, and the full `samples` list so a reviewer can reconstruct
any reduction without re-reading per-topology JSON. Failed
topologies (ratio undefined, non-positive, or non-finite) are
moved to `excluded` with the exclusion reason verbatim, and
their samples never feed any reducer.

### 2.5 Memory (peak RSS)

Both sidecars now carry `peak_rss_mb` + `peak_rss_source` +
`peak_rss_sample_count`. Batfish's peak is sampled via a
background `docker stats --no-stream` poller on the running
container while the solve window is active; Hammerhead's peak
is `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` taken around
the `simulate --emit-rib all` subprocess, normalised to MB
(bytes on Darwin/BSD, kilobytes on Linux). Both paths are
best-effort — `None` means the sampler was disabled
(`HAMMERHEAD_BENCH_DISABLE_PEAK_RSS=1`) or the reading was
unavailable; we never fake a zero. The per-trial mean rolls up
into `SimOnlyAgreement.batfish_peak_rss_mb` and
`SimOnlyAgreement.hammerhead_peak_rss_mb`; provenance (first
non-empty `peak_rss_source` across trials and the summed
`peak_rss_sample_count`) rides alongside on the same dataclass so
a reviewer can tell a 1-sample `rusage` point-in-time reading from
an N-sample `docker-stats` max-over-window. The corpus-level
`batfish_peak_rss_summary` + `hammerhead_peak_rss_summary` blocks
in `results/bench_summary.json` carry `max_mb` / `mean_mb` /
`source` / `sample_count` / `topology_count` across the run, with
`source: "mixed"` as a sentinel if topologies ever disagree on
sampler provenance — so the headline memory figures are one JSON
hop from the summary, not a walk of `topology_details`.

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
  snapshots — but it explains why the 2-node topologies show an
  inflated `wall_ratio`: most of Batfish's 22–30 s is JVM startup,
  not simulation. The headline `fair_ratio` defined in § 2
  **already excludes** JVM startup and snapshot upload on the
  Batfish side — numerator is `batfish_query_routes_s +
  batfish_query_bgp_s`, both pybatfish-reported inner times —
  so the `fair_ratio` column in the § 1 table is what a reader
  should cite when they want a solver-to-solver apples-to-apples
  comparison. The `wall_ratio` column is reported alongside as
  a conservative upper bound on the speedup an operator would
  observe end-to-end; it gaps `fair_ratio` the most at small
  topologies where JVM startup dominates, and the two converge
  as topology size grows and the JVM-startup share of Batfish
  wall-clock is amortised by accumulated solve time.
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
  routes on every node; Hammerhead elides them on topologies
  whose vendor adapters do not advertise loopbacks as separate
  /32s. This shows up as sub-100% `Presence` on the
  `acl-heavy-parse`, `ospf-p2p-3node`, `isis-l1l2-4node`,
  `ospf-broadcast-4node`, `mpls-l3vpn-4node`, and
  `mixed-vendor-frr-ceos-4node` rows of § 1, while the larger
  pure-Clos and BGP-only topologies sit at 100% presence
  because the /32-loopback set coincides. Because these
  prefixes are in `B \ H`, they are outside `B ∩ H` and do not
  enter the
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
  simulation at large sizes. The § 1 table's `Fair ratio`
  column excludes them on both sides (see § 2 definition —
  numerator is `batfish_query_routes_s + batfish_query_bgp_s`,
  denominator is `hammerhead_simulate_s + hammerhead_rib_total_s`);
  the `Wall ratio` column includes them on both sides. Both
  are reported so a reader can judge which one they care about.
- **One topology is still gated.** `acl-semantics-3node`
  requires cEOS-lab for the flow-level ACL audit and is
  excluded from the 16-topology count here.
- **Determinism.** Both tools produce byte-identical output
  across repeated runs on the same config directory, which we
  rely on as a sanity check but do not claim as a tested
  invariant of this artifact.
- **Harness migration history.** The Hammerhead-side denominator
  was rewritten 2026-04-22 against Hammerhead commit
  [`b46eb45`](https://github.com/optimesh-ai/hammerhead/commit/b46eb45),
  which introduced `hammerhead simulate --emit-rib all --format
  json`. The pre-migration harness shelled out to
  `hammerhead simulate` for topology + one `hammerhead rib
  --device <h>` subprocess per hostname to materialize FIBs
  (i.e. N+1 process launches per topology). The numbers shown
  here — including the headline `fair_ratio` aggregates and
  every per-topology row in § 1 — use the bulk-emit path, which
  issues exactly one subprocess per topology and folds RIB
  materialization into the single solve. `hammerhead_rib_total_s`
  is retained in `results/<topology>.json` as `0.0` for schema
  stability; the `asym_ratio` / `fair_ratio` fields therefore
  converge in the post-migration corpus. See
  `results/CHANGELOG.md` for the per-topology delta between the
  last pre-migration run and this one.

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
7. **Hammerhead** — `$HAMMERHEAD_CLI simulate --emit-rib all --format json`
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
uv run pytest                     # 219 passed, 1 skipped
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

- **The corpus tops out at 100 nodes for head-to-head numbers.**
  `spine-leaf-100node` is the largest topology the bench harness
  measures end-to-end (Batfish + Hammerhead, shared config
  directory, shared diff engine). The repo also ships a standalone
  5,120-switch fixture at `topologies/fat-tree-k64/` with
  Hammerhead-only timings — 9.3 s median total, 11.6 M FIB
  entries, ~6.6 GB peak RSS (`hammerhead profile`, 2026-04-22,
  see that fixture's README) — but it is **not** run through this
  bench's sim-only loop because marshalling 11.6 M routes through
  subprocess stdout + `json.loads` costs several GB on the Python
  side, on top of Hammerhead's own peak RSS; laptops OOM well
  before Batfish could finish. Batfish has been reported on
  networks of comparable size by its maintainers, but we have not
  measured it on 1,000+ -node configs *in this harness*. The
  **156× wall / 200× fair** ratio at 100 nodes is the most
  informative single datum for extrapolation; it is the smallest
  `wall_ratio` we report but sits near the corpus median on
  `fair_ratio`. Scale-regime readers should weight the
  20-/50-/100-node rows more heavily than the 2-/3-/4-node rows
  when extrapolating to production-sized fabrics.
- **Peak RSS instrumentation is best-effort.** Both sidecars
  now carry `peak_rss_mb` / `peak_rss_source` /
  `peak_rss_sample_count`. Batfish samples via `docker stats
  --no-stream` on a background poller (source
  `"docker-stats"`), Hammerhead via `resource.getrusage(
  RUSAGE_CHILDREN)` around the subprocess (source
  `"rusage"`). Readings can drop to `None` when the sampler is
  disabled (`HAMMERHEAD_BENCH_DISABLE_PEAK_RSS=1`), the
  container exits inside the first poll window, or the platform
  doesn't ship `resource`. A reviewer who sees `peak_rss_mb ==
  null` with a non-zero `peak_rss_sample_count` should treat it
  as measurement noise, not a bench bug.
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
