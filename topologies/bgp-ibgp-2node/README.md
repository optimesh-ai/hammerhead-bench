# bgp-ibgp-2node

2 nodes in a single AS, iBGP session between loopbacks, directly connected
via a transit /30.

**What it tests**

- Loopback-sourced iBGP peering (`neighbor ... update-source Loopback0`).
- Next-hop-self or the default "IGP via connected" resolution — both tools
  must agree on the resolved next-hop for each peer's advertised route.
- A simple happy path so a full harness pass here proves the pipeline
  itself works before harder topologies surface real semantic disagreements.

**Pass criteria**

- Vendor FIB and both tool FIBs carry identical route counts.
- For each of the two iBGP-learned prefixes the next-hop resolves to the
  correct transit IP on both sides.
- No BGP AS_PATH diff (iBGP never prepends).
