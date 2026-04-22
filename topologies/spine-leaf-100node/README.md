# spine-leaf-100node

5 spines + 95 leaves. 475 total links, 475 eBGP sessions. Every leaf
learns 94 remote loopbacks with 5 spine next-hops each (~44.7k ECMP
routes aggregate across the fabric).

**What it tests**

- The largest fixture in the corpus. Wall-clock separation between
  Hammerhead and Batfish is at its most pronounced here.
- Per-spine fan-out (95 sessions per spine, 95 interfaces).
- Next-hop set equality at 5-way ECMP.

**Pass criteria**

- All 95 leaves carry 94 remote loopbacks each with a 5-element
  next-hop set.
- Both simulators finish without running out of memory (`-Xmx4g` is
  tight but sufficient for Batfish).
