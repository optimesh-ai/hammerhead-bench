# mixed-vendor-frr-ceos-4node

4-node ring alternating FRR and Arista cEOS:
`r1 (FRR) — r2 (cEOS) — r3 (FRR) — r4 (cEOS) — r1`.

OSPFv2 point-to-point single area 0 underlay; iBGP AS 65100 full mesh
over loopbacks; `maximum-paths 2` (ECMP) so the ring geometry surfaces
on every destination.

**What it tests**

- Parser coverage across **two vendors in one snapshot** — FRR nodes
  emit `frr.conf` while cEOS nodes emit `startup-config`, with
  hammerhead auto-detecting the vendor per subdirectory.
- OSPFv2 + iBGP interaction: BGP next-hops are loopbacks; OSPF must
  resolve them via the ring, and the 2-direction ring must produce
  2-way ECMP.
- Vendor-specific knobs: cEOS `maximum-paths 2 ecmp 2` and FRR
  `maximum-paths ibgp 2` must both emit the same ECMP next-hop set.

**Pass criteria**

- Every node's FIB has 3 loopback /32s (the other three routers in
  the ring).
- Each of those routes has **2 next-hops**, one per ring direction.
- Next-hops on FRR and cEOS sides agree byte-for-byte between
  Hammerhead and Batfish.
- A mixed-vendor parse failure collapses to 0 routes on one side
  and would surface as a `presence` diff, not a `next_hop_match`
  diff — this fixture proves no such collapse happens.

**Why this matters for the bench**

Every other topology in this corpus is FRR-only. This is the fixture
that proves both simulators ingest an EOS `startup-config` in the
same snapshot alongside FRR `frr.conf` and still converge to the
same FIB — i.e. the vendor auto-detection pipeline works across
heterogeneous subdirectories, not just homogenous ones.
