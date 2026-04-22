# hub-spoke-wan-51node

1 hub + 50 branches, eBGP WAN star. Every branch peers eBGP to the hub on
its own /30 transit link. Hub AS 65000; branch N is AS 65000+N. Each
branch has a site prefix (10.N.0.0/24) and a loopback. Hub advertises
its loopback /32 + a `0.0.0.0/0` default downstream.

**What it tests**

- N-peer scale on a single device: 51 eBGP sessions on the hub (50
  branches + 1 for the hub itself). The per-device best-path evaluator
  must handle the fan-out without O(N²) blow-up.
- Classic WAN star topology: traffic between branches always traverses
  the hub. No branch-to-branch shortcuts.
- Route-map policy: inbound from each branch stamps LOCAL_PREF = 100
  (`FROM_BRANCH`); each branch stamps MED = 100 outbound to hub
  (`TO_HUB`).
- Default-route distribution: the hub originates `0.0.0.0/0` backed
  by Null0 and advertises it to every branch.

**Pass criteria**

- Hub FIB has 50 branch /24 prefixes via the matching per-branch /30
  transit next-hop.
- Each branch's FIB has 49 peer /24s (re-advertised via the hub) +
  the hub's loopback /32 + the `0.0.0.0/0` default, all next-hopping
  via the hub transit IP.
- No branch sees any other branch's /30 transit IP as a BGP next-hop —
  transit links are not advertised.
- Hammerhead and Batfish agree on prefix set + next-hop per row.

**Why this matters for the bench**

Every other corpus topology is a Clos, mesh, or small chain.
Enterprise WANs are stars. This is the fixture that proves the
benchmark isn't specialised to DC-style symmetric fabrics: both tools
must scale to N-peer single-node BGP session counts.
