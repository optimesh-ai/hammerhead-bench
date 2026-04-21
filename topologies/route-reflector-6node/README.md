# route-reflector-6node

2 route reflectors (RR1, RR2) + 4 clients (C1–C4) in a single AS. Clients
peer iBGP only to the RRs; the RRs peer iBGP to each other (backbone). Each
client originates one prefix.

**What it tests**

- BGP route reflector rules (RFC 4456): ORIGINATOR_ID + CLUSTER_LIST loop
  prevention. A client must not re-advertise its own prefix back to itself
  via the other RR.
- RR next-hop preservation: the RR reflects but does not rewrite next-hop.
- Cluster behaviour under redundant RRs.

**Pass criteria**

- Each client carries 3 remote prefixes (the other three clients').
- CLUSTER_LIST on each route has exactly the RR's cluster-id present once.
- No routes accidentally loop via the second RR.
