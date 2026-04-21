# acl-heavy-parse

3 nodes, BGP + OSPF. One of the three carries a 500-line ACL with
intentionally overlapping permit/deny rules (generated via
`scripts/generate_acl.py`). Routing is unaffected — this topology is about
**parse coverage**, not FIB correctness.

**What it tests**

- Does each tool ingest the full 500-line ACL without silently dropping
  entries? We measure `lines_in_config / lines_parsed` per tool.
- Vendor truth reports the ACL entry count via `show access-list`. Batfish
  and Hammerhead each report what they parsed. The three counts are diffed.

**Pass criteria**

- `lines_parsed` matches the vendor-reported entry count exactly on both
  Batfish and Hammerhead.
- No parser exception, no truncated line, no silent drop. The `unparsed_lines`
  counter (Hammerhead) / Batfish `parseWarnings` count is zero.

**Not in scope (phase 2, gated by `--with-acl-semantics`)**

Flow-level ACL audit comparison lives in a separate topology
(`acl-semantics-3node`) — that one asks "for this set of probe flows, which
are permitted/denied" and compares across vendor / Batfish / Hammerhead.
