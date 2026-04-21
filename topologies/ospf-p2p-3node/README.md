# ospf-p2p-3node

3 nodes R1–R2–R3 in a chain, point-to-point OSPF, all area 0.

**What it tests**

- OSPF SPF convergence on a linear topology.
- Cost metric accumulation (each /30 link has default cost; R1's route to R3
  should carry 2×cost).
- Point-to-point network type (no DR/BDR election).

**Pass criteria**

- R1's FIB has a route to R3's loopback with metric = 2 * (link-cost).
- Next-hop for R1→R3 is R2's transit IP on the R1↔R2 segment.
- No Type-2 LSAs generated (p2p doesn't use network LSAs).
