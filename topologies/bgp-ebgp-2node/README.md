# bgp-ebgp-2node

2 nodes in different ASes (65001, 65002), eBGP across a directly connected
link. No loopback peering, no multihop, no policy.

**What it tests**

- External-peering fundamentals: AS_PATH prepending on advertisement, AD 20
  for eBGP-learned routes, LOCAL_PREF not exchanged across the boundary.
- Rejects any implementation that accidentally exchanges LOCAL_PREF across
  an eBGP session (RFC 4271 §5.1.1).

**Pass criteria**

- Each peer carries exactly one eBGP-learned prefix from the other side.
- AS_PATH length = 1 on both sides (the other peer's AS).
- LOCAL_PREF not present on either side of the diff for the eBGP-learned route.
