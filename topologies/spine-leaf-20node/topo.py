"""spine-leaf-20node — 4 spines + 16 leaves, eBGP Clos with 4-way ECMP.

Medium-scale fixture: every leaf runs 16 eBGP sessions (4 × spine, 4 ×
leaf-side local) and learns 15 remote loopbacks with 4 spine next-hops
each, so the FIB surface is 60 ECMP routes per leaf + connected + local.

Generated from :mod:`topologies._shared.spine_leaf` — same template, same
addressing convention as ``spine-leaf-6node``, just sized up.
"""

from __future__ import annotations

from topologies._shared.spine_leaf import build_spine_leaf_bgp

SPEC = build_spine_leaf_bgp(
    name="spine-leaf-20node",
    description="4 spines + 16 leaves, eBGP ECMP across 4 uplinks per leaf.",
    num_spines=4,
    num_leaves=16,
)
