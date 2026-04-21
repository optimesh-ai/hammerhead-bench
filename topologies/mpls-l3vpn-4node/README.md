# mpls-l3vpn-4node

4 nodes: PE1 – P1 – P2 – PE2. One customer VRF "RED" with RD 100:1 and
route-targets `100:1` (import and export). OSPF for the IGP, LDP for label
distribution, MP-BGP for VPNv4.

**What it tests**

- L3VPN RT import/export: a prefix announced in VRF RED on PE2 must land in
  VRF RED on PE1 (and nowhere else).
- Recursive next-hop resolution: the PE-side VPNv4 next-hop resolves via the
  IGP to the P-router chain.
- Label-stack behaviour: vendor truth carries the outer LDP label + inner
  VPN label; the diff compares prefix presence, not label values (labels
  are dynamic).

**Pass criteria**

- PE1's VRF RED FIB carries PE2's VRF RED prefix, next-hop via P1.
- PE1's global FIB does NOT carry the VRF RED prefix.
- Tools report same recursive next-hop chain for the VPN route.
