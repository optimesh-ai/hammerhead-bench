# ospf-broadcast-4node

4 nodes on a shared broadcast segment (single clab bridge), OSPF `network
broadcast`, area 0. One node configured with `ip ospf priority 255` to pin
the DR election; a second configured with `priority 100` for the BDR.

**What it tests**

- DR/BDR election — both tools must agree on which node is DR.
- Type-2 network LSA generation.
- Route next-hop on a broadcast segment should be the advertising router's
  interface IP on the segment, not the DR.

**Pass criteria**

- DR elected by priority (not by tiebreaker) — both tools identify the
  correct DR.
- Routes to all three other loopbacks present on each node, all via the
  broadcast segment with the advertising router's interface as next-hop.
