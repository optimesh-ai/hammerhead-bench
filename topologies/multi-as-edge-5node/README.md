# multi-as-edge-5node

5 nodes — one local ISP (`local`, AS 65000) sitting between 2 upstream
transits (AS 100, 200), 1 settlement-free peer (AS 300), and 1 downstream
customer (AS 65001). Each remote AS originates its own /24, and `local`
applies the canonical internet-edge policy matrix.

**What it tests**

- Per-neighbour-class LOCAL_PREF:
  - customer → 200 (most preferred)
  - peer → 150
  - upstream → 100 (default)
- Per-neighbour-class outbound advertise-set filtering:
  - to customers: everything
  - to peers: own + customer routes only (community-list `CL_CUSTOMER`
    + `PL_OWN`)
  - to upstreams: own + customer routes only (same filter)
- Community tagging on ingress and egress filtering keyed on community
  values.
- Prefix-list + community-list composition inside a single route-map
  (`TO_PEER_OR_UPSTREAM` has two `permit` clauses, each with different
  match criteria).

**Pass criteria**

- `local` FIB carries all 5 prefixes. The customer /24 has LP=200; the
  peer /24 has LP=150; the upstream /24s have LP=100.
- `peer` and each `upstream_*` see exactly 2 prefixes from `local`:
  `local`'s own /24 and the customer /24. The upstream /24s must NOT
  leak to the peer (a "route leak" — the headline correctness bug at
  every real ISP edge).
- `customer` sees all 5 prefixes.
- Any LOCAL_PREF or community mismatch between Hammerhead and Batfish
  surfaces as an explicit `bgp_attrs_match = false` row in the diff.
