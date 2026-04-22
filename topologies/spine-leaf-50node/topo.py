"""spine-leaf-50node — 4 spines + 46 leaves, eBGP Clos with 4-way ECMP.

Larger-scale fixture. Each spine holds 46 eBGP sessions; each leaf 4.
Every leaf learns 45 remote loopbacks with 4 spine next-hops each,
so the aggregate FIB across all leaves is ~8,300 ECMP route-entries —
enough to surface any next-hop set ordering / presence drift that
small fixtures can't expose.

Generated from :mod:`topologies._shared.spine_leaf`.
"""

from __future__ import annotations

from topologies._shared.spine_leaf import build_spine_leaf_bgp

SPEC = build_spine_leaf_bgp(
    name="spine-leaf-50node",
    description="4 spines + 46 leaves, eBGP ECMP across 4 uplinks per leaf.",
    num_spines=4,
    num_leaves=46,
)
