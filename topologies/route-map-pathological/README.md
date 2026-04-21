# route-map-pathological

3 nodes (R1, R2, R3) forming a triangle. R1 announces a prefix to both R2 and
R3. R2 tags it with community `65000:100` inbound and rewrites LOCAL_PREF to
200 in a route-map. R3 tags it with `65000:200` inbound and rewrites
LOCAL_PREF to 150. R2 and R3 peer iBGP so both paths become candidates.

**What it tests**

- BGP best-path step by step: LOCAL_PREF wins over AS_PATH length. The R2
  path (LP 200) must win over the R3 path (LP 150) at R2's neighbours.
- Community propagation: downstream neighbours see the community tag
  attached by R2's route-map.
- Tiebreakers further down: if LOCAL_PREF were equal, AS_PATH → ORIGIN →
  MED → eBGP/iBGP → IGP metric → router-id.

**Pass criteria**

- At R2's downstream neighbour (or at R3 itself), the best path carries
  LOCAL_PREF = 200 and community `65000:100`.
- A LOCAL_PREF disagreement between Hammerhead and vendor surfaces as an
  explicit `bgp_attrs_match = false` row in the diff (this is a deliberate
  tripwire — a BGP implementation bug shows up here first).
