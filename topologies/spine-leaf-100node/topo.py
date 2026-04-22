"""spine-leaf-100node — 5 spines + 95 leaves, eBGP Clos with 5-way ECMP.

Largest fixture in the bench corpus. 500 links, 95 leaf ASes, every
leaf learns 94 remote loopbacks with 5 spine next-hops each. Aggregate
FIB surface across all 100 devices is ~45k route-entries — large
enough to make the wall-clock difference between Hammerhead and
Batfish measurable and meaningful, not just a constant-overhead noise
floor.

Generated from :mod:`topologies._shared.spine_leaf`.
"""

from __future__ import annotations

from topologies._shared.spine_leaf import build_spine_leaf_bgp

SPEC = build_spine_leaf_bgp(
    name="spine-leaf-100node",
    description="5 spines + 95 leaves, eBGP ECMP across 5 uplinks per leaf.",
    num_spines=5,
    num_leaves=95,
)
