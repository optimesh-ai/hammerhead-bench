# hammerhead-bench

An open benchmark corpus and measurement harness for empirical
head-to-head comparison of network control-plane simulators.
We compare **[Hammerhead](https://optimesh.ai)**
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
same rendered config directory) runs on a single Docker-capable
host. The `--with-truth` mode (live containerlab deployment +
per-device FIB extraction) is also implemented but requires Linux
and, for two topologies, the cEOS-lab image. This repository
currently publishes no numbers produced by this harness; § 1 says
which numbers we do stand behind and where they come from.

## 1. Results

**The April 2026 table is withdrawn.** The speed ratios and 100%
agreement table shown here from 2026-04-21 came from one laptop run
against an unpinned `batfish/allinone:latest` and could not be
reproduced; they were withdrawn on 2026-08-12.

The numbers below are the ones we currently quote. They come from
Hammerhead's own CI, not from this harness, and each one is re-run
on a schedule against a container image pinned by digest.

| What was measured | Against | Result | Hammerhead | Run, date | Re-run |
|---|---|---|---|---|---|
| Routes on a 10-router FRR lab (OSPF areas 0/1/2, IS-IS L2, iBGP with route reflectors, eBGP, redistribution), captured only after every adjacency is up and every RIB has been unchanged for 60 s | Live FRR routers: `quay.io/frrouting/frr:10.7.1@sha256:e995beaa50fdc9edb35eadcfefa29b7f062cc06f2b812613789b68fa541554d2` | **138 of 138 routes match real FRR on our 10-router lab (run 38685150242).** 0 FRR-only, 0 Hammerhead-only entries. Earlier runs scored 137: one route both sides drop was labelled differently. Same behaviour; we fixed the scorer. | main (`fa559f5e`) | 38685150242, 2026-10-15 | Weekly (Mondays); compared against the committed baseline from that run. Until 2026-10-14 this read 135 of 136: FRR was refusing two of the lab's config lines, and Hammerhead was not modelling them either. A refused line now fails the run, and with both lines fixed FRR and Hammerhead agree on the two routes they add |
| Routing tables over 351 datasets (2,040 devices) from Hammerhead's test corpora, demos and generated topologies; IPv4, default VRF, RIB only | Batfish `batfish/allinone@sha256:09817554db90e2f2674b72562c2d659427094187de0a3dfc23534ab58bf26207`, pybatfish 2025.7.7.2423 | **94.41%** of prefixes (267,262 of 283,084); egress interface agrees on **99.80%** of the prefixes both have. Over the 309 datasets Batfish parsed fully: 98.50% / 99.90% | `094c2391` | 38134221802, 2026-10-11 | Weekly (Sundays). The committed baseline (run 35944180044) has the same percentages, over 267,262 of 283,098 |
| Parity gate: four corpora (triangle, NX-OS, EOS, Junos), 12 devices, scored only over the devices the oracle answered for | Same Batfish digest | **37 of 38** prefixes; **37 of 37** egress interfaces | v0.2.0 (`8e884af2`) | 38218573680, 2026-10-12 | Every push to Hammerhead's main and weekly. The committed full report adds ASA and FRR corpora (14 devices): 44 of 47 prefixes, 44 of 44 egress interfaces (`ca8ef169`, 2026-08-28) |

**What these do not say.**

- The FRR row is one lab topology on one routing stack, not
  production hardware and not the other vendors Hammerhead parses.
  Its 39 router-local routes (each router's own addresses) are
  counted and not compared.
- The Batfish rows compare two models, not a model with routers.
  If both read a config the same wrong way, they agree on the wrong
  answer. Of the 16,359 disagreements in the conformance baseline,
  12,051 come from four datasets whose `frr.conf` Batfish reads with
  its IOS grammar.
- Agreement is only counted where Batfish produced an answer. A
  Batfish digest change makes the numbers incomparable, so the
  digest is bumped deliberately and the baseline re-recorded.

**Speed.** We publish no speed comparison with Batfish. The harness
still records wall-clock times and computes the ratios defined in
§ 2.3 for anyone who runs it; a figure from your own run describes
your host and your pins, and § 3 lists what it does not control for.

**Evidence.** Hammerhead's repository and CI are private. The run IDs
above identify the evidence records (result JSON, captured RIBs,
digests), which we share on request: <https://optimesh.ai/#trial>.

### 1.1 Ground truth through this harness (FRR subset)

`hammerhead-bench bench --frr-only-truth` extends the sim-only
diff into a 3-way comparison — *vendor truth T* (extracted from a
live containerlab deployment of the rendered configs), *Batfish
B*, and *Hammerhead H* — on the subset of topologies that are
runnable against FRR / Cumulus only. A topology qualifies when
every node's adapter kind is `frr` or `cumulus_vx`, the node
count is ≤ 20 (`harness/topology.py:FRR_ONLY_TRUTH_MAX_NODES`),
and the spec does not use the `external_renderer` escape hatch.
Topologies outside that subset fall back to sim-only in the same
run and appear with `truth_source: null` in
`results/<topology>.json`. The 3-way triad is `batfish_vs_truth`,
`hammerhead_vs_truth`, `batfish_vs_hammerhead`; each carries the
same four metrics as the sim-only agreement (`presence`,
`next_hop`, `protocol`, `bgp_attr`) as defined in § 2.

No run of this mode has been published. It needs Linux
(containerlab uses Docker + veth), and its FRR pin in
`versions.lock` is still `frrouting/frr:v8.4.1`, not the 10.7.1
image the table above uses. We will publish a table here only from
a CI run, never from hand-assembled numbers.

The `--frr-only-truth` mode is mutually exclusive with
`--sim-only` (the CLI exits non-zero if both are passed). All
other flags (`--only`, `--trials`, `--out`, …) work as before.

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

The harness computes the ratios below for anyone who runs it. We
do not publish them (§ 1).

**Infrastructure overhead vs. solver latency.** Batfish sits
behind a JVM + Jetty stack that takes tens of seconds to reach
REST readiness. The harness can run Batfish two ways:
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

**Fair ratio (primary).** With equivalent work on each side:

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
materialize it" — on the same two-phase pipeline shape. If you
report a speed figure from your own run, this is the ratio to
report, with the caveats in § 3. When someone wants
"what speedup does an operator see end-to-end on a fresh
container?" the `wall_ratio` field beside it is the right
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
  sensitive to small-topology noise (a 2-node rig's
  millisecond-scale wall time jitters enough to dominate a
  16-sample mean).
- **`geometric_mean`** — `exp(mean(log(ratios)))`, computed in
  log space for numerical stability. Unbiased central tendency
  on a multiplicative scale, and the one to use as "typical
  ratio" when the ratios span more than an order of magnitude.
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

**Hammerhead coverage.** From v0.2.2 Hammerhead says, per answer,
whether the configs hold lines its model does not read (§ 7). Not
to be confused with `coverage` / `mean_coverage` above, which is
the key-set Jaccard of § 2.2. A degraded answer is scored like any
other and stays in every mean and ratio summary: Hammerhead is the
tool under test, and dropping the topologies it read only in part
would flatter it. Each `bench_summary.json` instead carries a
`hammerhead_coverage_summary` block over the scored topologies:

- `complete_topology_count`, `degraded_topology_count` and
  `unknown_topology_count` (no recorded verdict: Hammerhead before
  v0.2.2, or no Hammerhead run);
- `degraded_topologies`, one `{topology, status, blockers,
  degraded_trials, trials}` entry per degraded topology, with
  Hammerhead's blockers verbatim;
- sim-only: `next_hop_agreement_mean_covered_complete`,
  `protocol_agreement_mean_covered_complete` and
  `bgp_attr_agreement_mean_covered_complete`, the `_covered` means
  repeated over the `covered_complete_topology_count` covered
  topologies whose coverage was complete;
- with-truth: `hammerhead_{presence,next_hop,protocol,bgp_attr}_match_rate_mean_complete`,
  the Hammerhead headline means over the complete-coverage
  topologies.

The complete-coverage means are `null`, never a vacuous 100%, when
no topology qualifies, which is every run against a Hammerhead
older than v0.2.2. A topology is degraded if any of its trials
was, and complete only if every trial recorded complete coverage.
The `--frr-only-truth` summary carries the counts and the list
(its fallback topologies also get the full block inside
`sim_only_summary`).

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
sampler provenance — so the corpus-level memory figures are one JSON
hop from the summary, not a walk of `topology_details`.

**What this does not claim.** Agreement between two simulators
is not correctness: if both tools misinterpret the same route-
map in the same direction, they will agree on the wrong answer.
Cross-validation against vendor ground truth is the job of
`--with-truth` mode; § 6 lists what we did and did not do there.

## 3. Threats to validity

These apply to any timing or agreement figure this harness
produces, including one from your own run.

- **A degraded Hammerhead answer is scored, not dropped.** When
  Hammerhead (v0.2.2+) reports that a topology's configs hold
  lines its model does not read, its RIBs are still diffed and
  counted, so an agreement figure can include answers built
  without those lines. `hammerhead_coverage_summary` (§ 2.4) says
  how many topologies that is and repeats the agreement means
  without them; the report flags each one. Timings and ratios are
  not coverage-filtered: a run that skipped lines may have done
  less work.

- **Variance is measured, but on one host.** The harness's
  wall-clock fields are `mean ± std` over `--trials 5` (the CLI
  default); per-trial timings are stored under
  `results/<topology>.json` `agreement.trials` + `trial_stats`
  for independent inspection. What we do *not* claim: that the
  distribution is Gaussian, that 5 samples resolve the tail, or
  that a different host would see the same spread. JVM
  first-pass class-loading is the usual source of Batfish
  variance on small topologies (seconds across reruns); it
  survives `--trials` because each trial starts a fresh
  container on purpose, so the measured std is an honest
  reflection of what a production user re-running a snapshot
  would see.
- **JVM cold-start is in the Batfish wall-clock.** Every
  Batfish run starts a fresh `batfish/allinone` container and
  pays the JVM startup cost (tens of seconds). This is
  realistic — most production Batfish users restart between
  snapshots — but it inflates `wall_ratio` on the 2-node
  topologies, where most of Batfish's wall time is JVM startup,
  not simulation. The `fair_ratio` defined in § 2
  **already excludes** JVM startup and snapshot upload on the
  Batfish side — numerator is `batfish_query_routes_s +
  batfish_query_bgp_s`, both pybatfish-reported inner times —
  so `fair_ratio` is the field to use for a solver-to-solver
  comparison. The `wall_ratio` field is reported alongside as
  a conservative upper bound on the speedup an operator would
  observe end-to-end; it gaps `fair_ratio` the most at small
  topologies where JVM startup dominates, and the two converge
  as topology size grows and the JVM-startup share of Batfish
  wall-clock is amortised by accumulated solve time.
- **One host per run.** A run measures the host it ran on.
  Absolute times are host-specific; *ratios* may be more
  portable, but we have not verified that.
- **Sim-only mode runs no convergence.** In sim-only mode,
  neither Batfish nor Hammerhead boots a real router, so
  timing does not include protocol convergence. Cross-
  validation against vendor-produced RIBs is the job of
  `--frr-only-truth` (§ 1.1); that mode is Linux-only (it uses
  containerlab + Docker) and is restricted to FRR-based
  topologies of ≤ 20 nodes, so it covers a subset of the
  corpus, not all of it.
- **Route-count asymmetry is a modeling difference, not
  disagreement.** Batfish materializes /32 loopback host
  routes on every node; Hammerhead elides them on topologies
  whose vendor adapters do not advertise loopbacks as separate
  /32s. This lowers `presence` on the IGP-heavy topologies
  (OSPF, IS-IS, L3VPN, the mixed-vendor snapshot) but not on
  the BGP-only Clos topologies, where the /32-loopback sets
  coincide. Because these
  prefixes are in `B \ H`, they are outside `B ∩ H` and do not
  enter the
  `next_hop_agree(t)` / `protocol_agree(t)` /
  `bgp_attr_agree(t)` metrics of § 2; they *do* lower
  `presence_agree(t)` (Jaccard) because they enlarge the union.
  Each `results/<topology>.json` therefore reports "presence"
  separately from the attribute agreement rates, so a reader can see
  exactly how much of any given gap is modeling asymmetry vs.
  substantive disagreement. This is a deliberate choice; we
  document it rather than post-process one tool's output to
  mirror the other's.
- **Batfish wall-clock includes pybatfish init + snapshot
  upload.** We do not subtract these. They are small next to
  JVM startup at small sizes and to simulation at large sizes.
  `fair_ratio` excludes them on both sides (see § 2 definition —
  numerator is `batfish_query_routes_s + batfish_query_bgp_s`,
  denominator is `hammerhead_simulate_s + hammerhead_rib_total_s`);
  `wall_ratio` includes them on both sides. Both are reported so
  a reader can judge which one they care about.
- **One topology is still gated.** `acl-semantics-3node`
  requires cEOS-lab for the flow-level ACL audit and is
  excluded from the 16-topology count here.
- **Determinism.** Both tools produce byte-identical output
  across repeated runs on the same config directory, which we
  rely on as a sanity check but do not claim as a tested
  invariant of this artifact.
- **Harness migration history.** The Hammerhead-side denominator
  was rewritten 2026-04-22 against Hammerhead commit `b46eb45`,
  which introduced `hammerhead simulate --emit-rib all --format
  json`. The pre-migration harness shelled out to
  `hammerhead simulate` for topology + one `hammerhead rib
  --device <h>` subprocess per hostname to materialize FIBs
  (i.e. N+1 process launches per topology). The harness now
  uses the bulk-emit path, which issues exactly one subprocess
  per topology and folds RIB
  materialization into the single solve. `hammerhead_rib_total_s`
  is retained in `results/<topology>.json` as `0.0` for schema
  stability; the `asym_ratio` / `fair_ratio` fields therefore
  converge. See `results/CHANGELOG.md` for the harness history.

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

## 7. Running the harness

The artifact runs on a single 16 GB+ host with Docker.

**Hammerhead needs a licence key from v0.2.0 on.** Request access at
<https://optimesh.ai/#trial>, then either run
`hammerhead license install <token>` or export `HAMMERHEAD_LICENSE`
in the shell you run the bench from (the harness passes its
environment to the Hammerhead subprocess). Without one, every
topology fails with `no Hammerhead licence found`; `make preflight`
warns about it.

```bash
git clone <this repo> hammerhead-bench && cd hammerhead-bench
export HAMMERHEAD_CLI=/path/to/hammerhead   # v0.2.0+ release binary
make preflight                        # sanity-checks the host, images and licence
make smoke                            # one topology end-to-end
make bench                            # full 16-topology corpus, sim-only
uv run hammerhead-bench report        # regenerate HTML + MD from results/
open results/report/report.html
```

Checked on 2026-10-15: the Hammerhead side of the harness runs
against the v0.2.1 release with a licence
(`bench --sim-only --no-batfish`, 15 of 16 topologies; `mpls-l3vpn-4node`
fails to parse on v0.2.0 and v0.2.1 alike); without a licence it fails
with the message above. The Batfish side was not re-run for this
revision.

From Hammerhead v0.2.2, `simulate --emit-rib all` exits 3 when the
configs hold lines the model does not read, after printing the full
answer, with a top-level `coverage` object saying so. The harness
scores that answer and flags the trial: `hammerhead_stats.json`
carries `coverage_status`, `coverage_degraded` and
`coverage_blockers` (all `null` against an earlier release). An exit
3 whose JSON does not say `coverage.degraded`, or any other non-zero
code, still fails the run.

The flag follows the score. Every per-topology agreement (and, on
the with-truth path, `results/<topology>.json` itself) rolls the
trials up into `hammerhead_coverage_status`,
`hammerhead_coverage_degraded` (`true` if any trial was degraded,
`false` only if every trial was complete, else `null`),
`hammerhead_coverage_blockers`, `hammerhead_coverage_degraded_trials`
and `hammerhead_coverage_trials`. `bench_summary.json` adds the
`hammerhead_coverage_summary` block of § 2.4. The Markdown report
gives each topology an `HH coverage` column, adds a
"coverage complete" column to the headline, and lists every
degraded topology with its blockers under "Hammerhead coverage";
the HTML report carries the same list. The bench also prints a
warning line for each degraded topology.

Each `results/<topology>.json` carries the topology, status, mode
and start/finish times, paths to both tools' per-node FIB dumps and
the per-prefix diff, and an `agreement` block: route and key counts,
`presence` / `presence_strict`, next-hop / protocol / BGP-attribute
agreement, the loopback policy and how many rows it reconciled,
per-trial and summary timings, the ratios of § 2.3, peak RSS, and
Hammerhead's coverage verdict (`hammerhead_coverage_*`, above).
It does not record tool versions; note `hammerhead --version` and
the Batfish digest yourself.

**What we pin.** In `versions.lock`: Batfish by digest (the same
`batfish/allinone@sha256:09817554…` image Hammerhead's parity and
conformance jobs use; the harness starts that image unless
`BATFISH_IMAGE` is set), FRR and cEOS by tag. Python dependencies are
ranges in `pyproject.toml`; `uv.lock` is not committed.

**What we do not pin.** The Hammerhead binary (whatever
`$HAMMERHEAD_CLI` points at), host kernel, Docker version and
hardware. Record them alongside any number you publish.

## 8. Tests

```bash
uv sync --all-extras --dev
uv run pytest                     # 299 passed, 1 skipped (2026-10-15)
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

- **The corpus tops out at 100 nodes for head-to-head runs.**
  `spine-leaf-100node` is the largest topology the bench harness
  runs end-to-end (Batfish + Hammerhead, shared config directory,
  shared diff engine). The repo also ships a standalone
  5,120-switch fixture at `topologies/fat-tree-k64/`, but it is
  **not** run through this bench's sim-only loop: marshalling
  millions of routes through subprocess stdout + `json.loads`
  costs several GB on the Python side, on top of Hammerhead's own
  peak RSS, and laptops run out of memory well before Batfish
  could finish. We publish no scale or timing figures for that
  fixture. Batfish has been reported on networks of comparable
  size by its maintainers; we have not measured it on 1,000+
  -node configs *in this harness*.
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
  (§ 3), and in limiting § 1 to re-runnable results with their
  caveats. The templates, the harness, and the diff engine are
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
  note   = {Commit \texttt{<the commit you used>}, accessed <date>}
}
```

When citing a number from § 1, cite the run ID, date and
Batfish or FRR digest given with it. When reporting your own run,
cite the `BATFISH_IMAGE` digest from `versions.lock` and the
output of `hammerhead --version`; the result JSON does not record
tool versions (§ 7).

## License

Apache-2.0.
