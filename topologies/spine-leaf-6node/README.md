# spine-leaf-6node

2 spines (S1, S2) + 4 leaves (L1–L4). Each leaf has eBGP unnumbered sessions
to both spines. Each leaf announces its loopback /32; each spine reflects
between leaves.

**What it tests**

- eBGP multipath (each leaf should see the other three loopbacks via ECMP
  across S1 and S2).
- BGP unnumbered on Linux (FRR-specific extension using IPv6 link-local
  next-hops re-resolved via RFC 5549).
- Next-hop set equality under ECMP — vendor, Batfish, and Hammerhead must
  agree on BOTH spine next-hops, not just one.

**Pass criteria**

- All 4 leaves carry 3 remote-loopback routes, each with 2 next-hops (S1, S2).
- ECMP reorder does not produce a diff (canonicalization asserts this).
- No path withdrawn during the 15 s stability window.
