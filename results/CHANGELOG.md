# results/ changelog

Running log of corpus regenerations, with the commit that drove each
regen and a one-paragraph summary of how the numbers moved. Per-topology
JSON files in this directory are the source of truth; this file exists
to make it obvious *which* run produced them.

## 2026-04-22 — bulk-emit migration (Hammerhead `b46eb45`)

**What changed in the harness:** `harness/tools/hammerhead.py` stopped
shelling out to `hammerhead rib --device <h>` once per device and now
issues a single `hammerhead simulate --emit-rib all --format json` call
per topology. See the `HammerheadRunner` protocol rewrite and the
`_default_hammerhead_hook` expected-hostnames gate in `harness/cli.py`.
The `ASYM_RATIO_NOTE` constant at `harness/pipeline.py:392` was updated
in-band to explain the new post-migration semantics
(`asym_ratio == fair_ratio`) and the backward-compatible `rib_total_s
== 0.0` schema field. Downstream consumers of `results/<topology>.json`
do not need to branch — the JSON shape is preserved.

**Why the numbers moved.** The per-device subprocess loop incurred ~20
ms of process-launch + topology-reload overhead per hostname, which at
100 nodes amounted to ~9 s of fixed cost that had nothing to do with
control-plane solve. The bulk-emit path parses once, simulates once,
and materializes all FIBs in a single pass through
`Pipeline::build`.

**Per-topology delta (pre → post migration, `fair_ratio` column):**

| Topology                    | Pre-migration | Post-migration | Lift   |
|-----------------------------|--------------:|---------------:|-------:|
| bgp-ebgp-2node              |       157.7×  |        198.5×  |  1.26× |
| bgp-ibgp-2node              |       142.4×  |        206.1×  |  1.45× |
| acl-heavy-parse             |       127.4×  |        217.0×  |  1.70× |
| ospf-p2p-3node              |       119.4×  |        219.4×  |  1.84× |
| route-map-pathological      |       102.4×  |        215.2×  |  2.10× |
| isis-l1l2-4node             |        93.8×  |        168.4×  |  1.80× |
| mixed-vendor-frr-ceos-4node |       102.5×  |        187.4×  |  1.83× |
| mpls-l3vpn-4node            |       122.8×  |        253.2×  |  2.06× |
| multi-as-edge-5node         |        91.6×  |        229.3×  |  2.50× |
| ospf-broadcast-4node        |       103.7×  |        214.6×  |  2.07× |
| spine-leaf-6node            |        93.9×  |        235.5×  |  2.51× |
| route-reflector-6node       |        87.7×  |        250.9×  |  2.86× |
| spine-leaf-20node           |        54.5×  |        362.4×  |  6.65× |
| spine-leaf-50node           |        19.7×  |        299.7×  | 15.21× |
| hub-spoke-wan-51node        |        16.3×  |        259.2×  | 15.90× |
| spine-leaf-100node          |         6.8×  |        199.8×  | 29.38× |

Corpus-wide `fair_ratio`: **mean 90× → 232×** (median 98× → 218×);
the 100-node spine-leaf went from the weakest point on the curve to
roughly the corpus median. `wall_ratio` mean 356× → 696×. 100%
next-hop / protocol / BGP-attribute agreement preserved on every
topology.

**Reproducer.** Build Hammerhead at commit `b46eb45` or later; set
`$HAMMERHEAD_CLI` to the `target/release/hammerhead` binary; run
`./scripts/bench_one_by_one.sh --trials 5` from the bench repo root.
The 16 per-topology JSON files in this directory were produced by
exactly that invocation on an Apple M-series laptop.
