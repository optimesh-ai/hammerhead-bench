# spine-leaf-50node

4 spines + 46 leaves. 184 total links, 184 BGP sessions. Every leaf
learns 45 remote loopbacks with 4 spine next-hops each (180 ECMP routes
per leaf, ~8.3k aggregate).

**What it tests**

- Larger-scale eBGP: wall-clock separation between simulators becomes
  measurable here.
- ECMP next-hop set equality at scale.
- Session fan-out per spine (46 sessions per spine).

**Pass criteria**

- All 46 leaves carry 45 remote loopbacks each with a 4-element
  next-hop set.
- No simulator runs out of heap (Batfish `-Xmx4g` still fits).
