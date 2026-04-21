# hammerhead-bench

A reproducible benchmark comparing **[Hammerhead](https://github.com/optimesh-ai/hammerhead)**, **[Batfish](https://batfish.org)**, and **vendor ground truth** (FRR, Arista cEOS-lab) across a corpus of network topologies. Measures **accuracy** (FIB diff vs. the actual device) and **speed** (wall-clock + peak RSS). Static HTML report.

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
| `spine-leaf-6node` | 6 | 2 spines × 4 leaves, eBGP unnumbered |
| `route-reflector-6node` | 6 | 2 RRs + 4 clients, iBGP |
| `mpls-l3vpn-4node` | 4 | PE–P–P–PE, one VRF, RT import/export |
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
