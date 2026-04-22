# fat-tree-k64

**5,120-switch fat-tree(k=64) DC underlay, single-area OSPFv2.**

- 1,024 core (Arista EOS) + 2,048 agg (Arista EOS) + 2,048 edge (Cumulus/FRR)
- 131,072 P2P /30 links, one OSPFv2 adjacency per link
- Loopbacks out of `10.0.0.0/12`; P2Ps out of `10.128.0.0/10`
- Deterministic: every regen produces byte-identical configs

## Why this shape

Fat-tree(k) is the canonical scale test for DC fabrics:
- `(k/2)^2` core × `k` pods × `k/2` agg-per-pod × `k/2` edge-per-pod
- k=64 → **5,120 switches, 131,072 links** — matches Forward Networks'
  advertised 10k-device ceiling once routes materialise
  (5,120 switches × ~30 loopback /32s = ~150k installed routes per run)

## Sim-only only

This fixture uses the `TopologySpec.external_renderer` escape hatch.
`--with-truth` is not supported — no clab YAML is rendered, and
standing up 5,120 containerised switches isn't viable on developer
hardware.

## Known bench-harness limit

The current `hammerhead-bench` per-device FIB extraction loop invokes
`hammerhead rib --device <hostname>` once per device. That's fine at
≤100 nodes (the baseline 16 topologies) but at 5,120 nodes it is
`5,120 × (parse + simulate + dump-one)` sequential subprocesses — hours
of wall-clock. Until the harness is extended with a batch extraction
path (one `simulate --format json` dump → per-device slices), **run
Hammerhead directly on the fixture** to capture real numbers:

```bash
python3 topologies/_shared/fat_tree.py  # optional: regenerate configs
hammerhead simulate \
    results/workdir/fat-tree-k64/configs --format json
```

## Measured numbers (Apple M-series arm64, 2026-04-22, release build)

3-run median via `hammerhead profile --format json`:

| phase     | median (ms) | min  | max  |
|-----------|------------:|-----:|-----:|
| parse     |       194.3 |  185 |  235 |
| topology  |        53.9 |   53 |   56 |
| ospf      |       884.0 |  837 |  935 |
| fib_merge |     3,042.0 |2,968 |3,740 |
| query     |     4,834.1 |4,631 |5,114 |
| **total** |   **9,255.6** |9,059 |9,449 |

- **Devices:** 5,120 (1,024 core + 2,048 agg + 2,048 edge)
- **Links:** 67,584 P2P /30 (fat-tree intra-pod + pod↔core)
- **Installed FIB entries:** 11,632,640 (~11.6 M)
- **Peak RSS:** ~6.6 GB (median across 3 runs)

## Reproduce (fixture only, no bench harness)

```bash
# regenerate configs (deterministic; takes ~2s)
python3 -c "from pathlib import Path; \
    from topologies._shared.fat_tree import generate_fat_tree; \
    generate_fat_tree(64, Path('/tmp/ft64_cfgs'))"

# run Hammerhead
/usr/bin/time -l hammerhead profile /tmp/ft64_cfgs --format json
```

## Reproduce (through bench harness, sim-only, Hammerhead-only)

Warning: currently ~hours because of the per-device RIB extraction
loop. Fine for validating the fixture wiring; not useful for
scale timing.

```bash
HAMMERHEAD_CLI=/path/to/hammerhead \
    ./.venv/bin/hammerhead-bench bench --sim-only \
    --only fat-tree-k64 --no-batfish -v
```
