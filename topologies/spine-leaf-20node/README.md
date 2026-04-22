# spine-leaf-20node

4 spines + 16 leaves in a full Clos fabric. Each leaf uplinks to every
spine; each leaf carries its own AS (65101..65116) and the spines
share AS 65000. Generated programmatically from
`topologies/_shared/spine_leaf.py`.

**What it tests**

- Medium-scale eBGP (64 total sessions).
- 4-way ECMP per remote loopback, 15 remote loopbacks per leaf.
- FIB surface per leaf = 60 BGP routes + 4 connected + 1 local.

**Pass criteria**

- Every leaf carries 15 remote loopbacks each with a 4-element next-hop
  set (one /30 to each spine).
- Next-hop set equality under canonicalization.
