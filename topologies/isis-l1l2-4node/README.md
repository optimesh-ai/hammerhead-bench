# isis-l1l2-4node

4 nodes: 2 × L1-only + 2 × L1L2 on the boundary. Wide metrics (RFC 5305),
NET areas `49.0001` (L1 + L1L2) and `49.0002` (L2-only side if we grow it).

**What it tests**

- IS-IS L1/L2 adjacency hierarchy.
- L1-preferred tie-breaker (RFC 1195): inter-area traffic out of L1 prefers
  the closest L1L2 via L1 default before falling back to L2.
- Wide-metric TLV 22 presence; narrow-metric TLV 2 absence.

**Pass criteria**

- L1 router sees a default route injected from an L1L2 router (ATT bit).
- L2 next-hop resolution matches between vendor and tool.
- No narrow-metric LSPs present in any of the three outputs.
