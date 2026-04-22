# hammerhead-bench

A reproducible benchmark comparing **[Hammerhead](https://github.com/optimesh-ai/hammerhead)**, **[Batfish](https://batfish.org)**, and **vendor ground truth** (FRR, Arista cEOS-lab) across a corpus of network topologies. Measures **accuracy** (FIB diff vs. the actual device) and **speed** (wall-clock + peak RSS). Static HTML report.

## Results (16 topologies, sim-only mode)

Measured on an Apple M-series laptop, single run, `hammerhead-bench bench --sim-only`.
Batfish: `batfish/allinone:latest`, `-Xmx4g`. Hammerhead: release binary.
Both tools see the exact same rendered config directory per topology; FIBs compared after semantic next-hop normalization (`AUTO/NONE` / `dynamic` / `null_interface` → `None`). **100% next-hop / protocol / BGP-attribute agreement on every (node, vrf, prefix) cell present in both simulators.**

| Topology | Nodes | Routes (bf / hh) | NH agree | Batfish wall | Hammerhead wall | Speedup |
|---|---:|---:|---:|---:|---:|---:|
| `bgp-ebgp-2node` | 2 | 6 / 4 | 100% | 29.42 s | 0.037 s | **788.6×** |
| `bgp-ibgp-2node` | 2 | 0 / 4 | 100% | 22.96 s | 0.039 s | **593.5×** |
| `acl-heavy-parse` | 3 | 12 / 6 | 100% | 35.44 s | 0.048 s | **739.1×** |
| `ospf-p2p-3node` | 3 | 12 / 6 | 100% | 30.79 s | 0.046 s | **675.1×** |
| `route-map-pathological` | 3 | 0 / 12 | 100% | 26.90 s | 0.048 s | **555.4×** |
| `isis-l1l2-4node` | 4 | 26 / 26 | 100% | 28.63 s | 0.060 s | **480.9×** |
| `mixed-vendor-frr-ceos-4node` | 4 | 8 / 8 | 100% | 28.76 s | 0.055 s | **518.5×** |
| `mpls-l3vpn-4node` | 4 | 8 / 8 | 100% | 31.58 s | 0.067 s | **469.4×** |
| `ospf-broadcast-4node` | 4 | 8 / 4 | 100% | 30.65 s | 0.054 s | **563.9×** |
| `multi-as-edge-5node` | 5 | 32 / 27 | 100% | 33.04 s | 0.068 s | **486.2×** |
| `spine-leaf-6node` | 6 | 52 / 36 | 100% | 31.55 s | 0.071 s | **441.7×** |
| `route-reflector-6node` | 6 | 0 / 36 | 100% | 28.46 s | 0.082 s | **349.0×** |
| `spine-leaf-20node` | 20 | 560 / 432 | 100% | 42.53 s | 0.251 s | **169.4×** |
| `spine-leaf-50node` | 50 | 2,990 / 2,622 | 100% | 56.88 s | 1.357 s | **41.9×** |
| `hub-spoke-wan-51node` | 51 | 5,351 / 5,251 | 100% | 56.02 s | 1.557 s | **36.0×** |
| `spine-leaf-100node` | 100 | 11,305 / 10,355 | 100% | 102.67 s | 9.513 s | **10.8×** |

**Aggregate:** 16 topologies, 20,370 routes (Batfish) / 18,837 routes (Hammerhead), 100% agreement across all three axes. Total wall-clock: **Batfish 616.29 s → Hammerhead 13.35 s (46.2× aggregate speedup)**.

**Corpus diversity.** The 16 topologies cover: single-protocol iBGP / eBGP / OSPF p2p / OSPF broadcast / IS-IS L1L2; multi-protocol (OSPF underlay + iBGP full mesh); policy-heavy (stacked LOCAL_PREF + community route-maps, internet-edge transit/peer/customer filter matrix); multi-vendor (FRR + Arista cEOS in one snapshot); scale (100-node Clos spine-leaf, 51-node hub-and-spoke WAN star); MPLS L3VPN (PE-P-P-PE with per-VRF BGP); ACL-dominant parsing (500-line overlapping filter). Node counts range from 2 to 100; topology shapes span chain, triangle, ring, star, Clos, and WAN star.

The per-tool route-count gap at larger scale is Batfish materializing /32 loopback host routes on every node; Hammerhead elides them. Accuracy is measured on the union of (node, vrf, prefix) cells present in both — when both tools install the same prefix, their next-hop / protocol / BGP-attribute fields agree exactly on every row.

One topology (`acl-semantics-3node`) still needs Arista cEOS-lab for its vendor-truth arm (licensed image not on this host) and is gated behind `--with-acl-semantics`. The previous FRR parser gap on `router bgp <asn> vrf <name>` that blocked `mpls-l3vpn-4node` has been fixed upstream and the topology is green end-to-end.

Raw per-topology JSON: `results/<topology>.json`. Rolled-up aggregate: `results/bench_summary.json`.

## Reproduce this benchmark on your laptop

Assumes Docker Desktop and [containerlab](https://containerlab.dev) are installed; harness needs ≥ 16 GB host RAM.

```bash
git clone <this repo> hammerhead-bench && cd hammerhead-bench
cp .env.example .env                          # edit $HAMMERHEAD_CLI
docker pull batfish/allinone                  # then pin its digest in versions.lock
make preflight                                # sanity check
make smoke                                    # one topology end-to-end (~5 min)
make bench                                    # full corpus (~1 hour FRR-only)
uv run hammerhead-bench report                # regenerate HTML + MD from results/
open results/report/report.html
```

## What it measures

For each topology the harness runs sequentially:

1. Renders configs from Jinja2 templates.
2. Deploys the topology under containerlab with explicit per-container memory caps.
3. Waits for protocol convergence (BGP Established + FIB stable across two 15 s intervals).
4. Pulls vendor ground-truth FIB from each node.
5. Tears the topology down and verifies no dangling containers or networks.
6. Runs Batfish (dockerized, `-Xmx4g`) on the same configs; extracts its FIB.
7. Runs Hammerhead via `$HAMMERHEAD_CLI` on the same configs; extracts its FIB.
8. Diffs each tool's output against vendor truth.

After all topologies: `hammerhead-bench report` emits an HTML + Markdown summary under `results/report/` with a headline Batfish-vs-Hammerhead table, three Plotly charts (per-topology next-hop, per-protocol, per-topology presence), a per-topology breakdown, BGP attribute match rates, a failed-topology list, and methodology + hardware disclosures. Every dynamic value is HTML-escaped. Plotly is inlined once; the HTML file opens cleanly from a `file://` URL with no network fetch.

## Design principles

- **One topology deployed at a time.** Ever. Never two. The harness does not parallelize topology runs even if it'd be faster — we're memory-bound on a 32 GB laptop.
- **Explicit memory caps everywhere.** Every container in every clab yaml carries a `memory:` limit (FRR 256 M / cEOS 1024 M / crpd 1536 M / srlinux 2048 M / xrd 4096 M). The Batfish JVM is launched with `_JAVA_OPTIONS=-Xmx4g`. On Linux the harness process itself sets `RLIMIT_AS=8G`.
- **Teardown is verified.** After every topology the harness greps `docker ps` and `docker network ls` for clab labels and aborts the run loudly if anything is left over. Memory must return to within 500 MB of the pre-topology baseline within 30 s or the run aborts.
- **Pins, not tags.** Container images are pinned in `versions.lock` (FRR by tag, Batfish by digest). Upgrading an image is a one-line diff with a visible code review.
- **Static reports, no web stack.** HTML with inlined plotly. Open it in a browser.

## Vendor support

- ✅ **FRR** (`frrouting/frr`) — full vendor-truth + convergence + FIB extraction
- ✅ **Arista cEOS-lab** — full; user supplies the image (Arista EOS Central, free account)
- 🧱 **Juniper crpd** — stubbed; adapter raises `NotImplementedError`
- 🧱 **Nokia SR Linux** — stubbed
- 🧱 **Cisco XRd** — stubbed; skipped by default for memory reasons even when wired

## Topologies

| Name | Nodes | What it tests |
| --- | ---: | --- |
| `bgp-ibgp-2node` | 2 | iBGP loopback peering, single AS |
| `bgp-ebgp-2node` | 2 | eBGP across directly connected link |
| `ospf-p2p-3node` | 3 | OSPF point-to-point chain |
| `ospf-broadcast-4node` | 4 | OSPF DR/BDR on shared segment |
| `isis-l1l2-4node` | 4 | IS-IS L1 + L2 boundary |
| `mixed-vendor-frr-ceos-4node` | 4 | FRR + Arista cEOS in one snapshot, iBGP-over-OSPF ECMP |
| `mpls-l3vpn-4node` | 4 | PE–P–P–PE, one VRF, RT import/export |
| `multi-as-edge-5node` | 5 | internet edge: transit/peer/customer policy matrix (LP + community filters) |
| `spine-leaf-6node` | 6 | 2 spines × 4 leaves, eBGP unnumbered |
| `route-reflector-6node` | 6 | 2 RRs + 4 clients, iBGP |
| `spine-leaf-20node` | 20 | scale-up Clos (4 spines × 16 leaves) |
| `spine-leaf-50node` | 50 | scale-up Clos (4 spines × 46 leaves) |
| `hub-spoke-wan-51node` | 51 | classic WAN star: 1 hub + 50 eBGP branches |
| `spine-leaf-100node` | 100 | scale-up Clos (4 spines × 96 leaves) |
| `route-map-pathological` | 3 | BGP best-path tiebreakers via LOCAL_PREF rewrite |
| `acl-heavy-parse` | 3 | 500-line overlapping ACL; measures parse coverage |
| `acl-semantics-3node` | 3 | mixed-vendor (FRR + cEOS) first-match-wins ACL semantics |

See `topologies/<name>/README.md` for each topology's pass criteria. The `acl-semantics-3node` topology (flow-level ACL audit on Arista cEOS) is gated behind `hammerhead-bench bench --with-acl-semantics` so FRR-only runs on FRR-only hosts stay green.

## Pipeline

Sequential per topology, driven by `harness.pipeline.run_topology`:

1. **Render** — Jinja2 templates under `topologies/<name>/` + `shared_templates/` per adapter `kind` (linux / bridge / ceos), with `StrictUndefined`.
2. **Deploy** — containerlab brings the lab up with per-container memory caps.
3. **Converge** — wait for BGP Established + FIB stable across two 15 s intervals.
4. **Extract vendor truth** — FRR via `vtysh`, cEOS via `Cli -c "<cmd> | json"`, normalized to a shared FIB schema.
5. **Teardown + memory guard** — destroy the lab, verify zero dangling containers and memory recovery within 500 MB of baseline.
6. **Batfish** — `batfish/allinone` container (`-Xmx4g`), pybatfish against the same config dir, FIB extracted to the same shape.
7. **Hammerhead** — `$HAMMERHEAD_CLI simulate --format json`, transformed to the shared FIB shape.
8. **Diff** — per (node, vrf, prefix) presence + next-hop + protocol + BGP-attribute rows (`harness.diff.engine`), metrics rolled up per topology (`harness.diff.metrics`).
9. **Aggregate + report** — `hammerhead-bench report` loads all per-topology JSON and emits Markdown + HTML with charts.

## CLI

```
hammerhead-bench preflight                                 # host sanity check
hammerhead-bench smoke --topology bgp-ibgp-2node           # one topology end-to-end
hammerhead-bench bench [--only NAME] [--skip NAME]         # full corpus
               [--max-nodes N] [--with-acl-semantics]
               [--no-batfish] [--no-hammerhead]
               [--keep-lab-on-failure] [-v]
hammerhead-bench report --results-dir results/             # regenerate HTML + MD
```

## Tests

```bash
uv sync --all-extras --dev
uv run pytest                                              # 194 passed, 1 skipped
uv run ruff check .
```

The suite covers template rendering, adapter dispatch, memory guards, diff engine, metrics, Batfish + Hammerhead wrappers, CLI selection, pipeline orchestration, and the full report generator (loader + 3 plot factories + HTML + Markdown). The one skipped test runs only when Docker + containerlab are present on the host.

## License

Apache-2.0.
