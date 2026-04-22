"""fat-tree-k64 — k=64 fat-tree DC underlay, 5,120 switches.

This is a scale fixture: 1,024 core EOS + 2,048 agg EOS + 2,048 edge
FRR = **5,120 devices**, 131,072 P2P /30 links, single-area OSPFv2.
Far too large to express as ``tuple[Node, ...]`` + ``tuple[Link, ...]``,
so it uses the ``TopologySpec.external_renderer`` escape hatch —
``generate_fat_tree(64, configs_dir)`` emits every ``<host>.cfg``
(core + agg) and ``<host>/frr.conf`` (edge) directly into the
harness's ``configs/`` directory, and the Jinja path is skipped.

Sim-only only. ``--with-truth`` fails loudly because no clab YAML
is rendered and no containerlab could plausibly stand up 5,120
switches on a developer laptop anyway.

Addressing matches the hammerhead main-repo
``tools/benchmarks/fat_tree.py`` byte-for-byte so cross-repo numbers
are directly comparable.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from harness.topology import TopologySpec
from topologies._shared.fat_tree import generate_fat_tree

SPEC = TopologySpec(
    name="fat-tree-k64",
    description=(
        "Fat-tree(k=64) DC underlay — 1,024 core + 2,048 agg + 2,048 edge "
        "= 5,120 switches, single-area OSPFv2, sim-only."
    ),
    nodes=(),
    links=(),
    template_dir=Path(__file__).resolve().parent,
    external_renderer=partial(generate_fat_tree, 64),
)
