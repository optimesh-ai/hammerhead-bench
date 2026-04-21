# acl-semantics-3node

3-router triangle, **r2 is Arista cEOS**, r1 and r3 are FRR. r2 carries a
hand-curated ACL with overlapping permit/deny entries. The benchmark is a
flow-audit diff: for a fixed probe set of (src, dst, sport, dport, proto),
which of permit/deny does each tool report at r2?

**Gated.** Not included in the default `bench` run because it requires the
Arista cEOS image to be pulled locally. Run with::

    uv run bench --with-acl-semantics

**What it tests**

- Vendor ground truth: probe generated via `packet-tracer` / `tcpdump` on cEOS.
- Batfish: `testFilters` reachability query.
- Hammerhead: `acl-audit` for the same (src, dst, sport, dport, proto).
- The three verdicts per probe must match; any `tool_reports_permit_but_vendor_denies`
  is a blocker finding.

**Status**

Scaffold only. The cEOS adapter ships in phase 8; until then this directory
contains a stub topology file that intentionally raises `NotImplementedError`
at import time so accidental inclusion in a bench run fails loud.
