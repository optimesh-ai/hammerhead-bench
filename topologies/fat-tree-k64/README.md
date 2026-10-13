# fat-tree-k64

**5,120-switch fat-tree(k=64)-shaped DC underlay, single-area OSPFv2.**

- 1,024 core (Arista EOS) + 2,048 agg (Arista EOS) + 2,048 edge (Cumulus/FRR)
- 67,584 P2P /30 links, one OSPFv2 adjacency per link:
  65,536 agg↔edge (full mesh inside each of the 64 pods) plus 2,048
  agg↔core (each agg has a single core uplink, so each core serves
  two aggs; a canonical fat-tree would give every agg k/2 uplinks)
- Loopbacks out of `10.0.0.0/12`; P2Ps out of `10.128.0.0/10`
- Deterministic: every regen produces byte-identical configs

## Why this shape

Fat-tree(k) is the canonical scale shape for DC fabrics:
`(k/2)^2` core, `k` pods, `k/2` agg and `k/2` edge per pod. At k=64
that is 5,120 switches. The thinned core layer keeps the config set
and the link count manageable while preserving the device count and
the pod structure.

## Sim-only only

This fixture uses the `TopologySpec.external_renderer` escape hatch.
`--with-truth` is not supported — no clab YAML is rendered, and
standing up 5,120 containerised switches isn't viable on developer
hardware.

## Known bench-harness limit

The harness gets every FIB from one
`hammerhead simulate --emit-rib all --format json` call, then parses
that output in Python. At this size the output holds millions of
routes, and holding it in Python takes several GB on top of
Hammerhead's own peak RSS, so the bench loop runs out of memory on
typical laptops. Run Hammerhead directly on the fixture instead.

We publish no timing or memory figures for this fixture.

## Reproduce (fixture only, no bench harness)

```bash
# regenerate configs (deterministic)
python3 -c "from pathlib import Path; \
    from topologies._shared.fat_tree import generate_fat_tree; \
    generate_fat_tree(64, Path('/tmp/ft64_cfgs'))"

# run Hammerhead (v0.2.0+ needs a licence; see the top-level README § 7)
hammerhead simulate /tmp/ft64_cfgs --format json > /dev/null
```

## Reproduce (through bench harness, sim-only, Hammerhead-only)

Expect it to exhaust memory on a laptop (see above). Useful for
validating the fixture wiring on a large host.

```bash
HAMMERHEAD_CLI=/path/to/hammerhead \
    uv run hammerhead-bench bench --sim-only \
    --only fat-tree-k64 --no-batfish -v
```
