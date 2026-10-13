# results/ changelog

Running log of harness changes that affect results, and of corpus runs
that were published. A published run names the commit that drove it, the
Hammerhead version and the Batfish digest. Per-topology result files are
not committed (`results/*` is git-ignored); see README § 1 for the
numbers we publish.

## 2026-10-13 — April numbers withdrawn; Batfish pinned by digest

**Withdrawn.** The per-topology speed ratios and 100% agreement figures
from the 2026-04-21/22 runs (previously in README § 1 and in the entry
below) were withdrawn on 2026-08-12 and are removed here. They came
from one laptop run against an unpinned `batfish/allinone:latest` and
could not be reproduced; the result files were never committed.
README § 1 now quotes only results that are re-run on a schedule, each
with its run ID, Hammerhead version and oracle digest.

**Harness changes.**

- `versions.lock` pins `BATFISH_IMAGE` to
  `batfish/allinone@sha256:09817554db90e2f2674b72562c2d659427094187de0a3dfc23534ab58bf26207`,
  the oracle Hammerhead's parity and conformance jobs use, and
  `BatfishConfig` now starts that image (or `$BATFISH_IMAGE`). Before
  this, the harness always started `batfish/allinone:latest`.
- `make preflight` reports whether a Hammerhead licence is present
  (v0.2.0+ release builds refuse to run without one) and no longer
  crashes on a host without the docker CLI.
- `make bench`, `make bench-fast` and `make report` run the harness
  instead of exiting with a stub message.

Checked against the Hammerhead v0.2.0 release (sim-only, no Batfish):
the harness's `simulate --emit-rib all --format json` contract is
unchanged.

## 2026-04-22 — bulk-emit migration

**What changed in the harness:** `harness/tools/hammerhead.py` stopped
shelling out to `hammerhead rib --device <h>` once per device and now
issues a single `hammerhead simulate --emit-rib all --format json` call
per topology. See the `HammerheadRunner` protocol rewrite and the
`_default_hammerhead_hook` expected-hostnames gate in `harness/cli.py`.
The `ASYM_RATIO_NOTE` constant in `harness/pipeline.py` was updated
in-band to explain the new post-migration semantics
(`asym_ratio == fair_ratio`) and the backward-compatible `rib_total_s
== 0.0` schema field. Downstream consumers of `results/<topology>.json`
do not need to branch — the JSON shape is preserved.

**Why timings moved.** The per-device loop paid process launch and a
topology reload once per hostname, a fixed cost that grew with node
count and had nothing to do with control-plane solve. The bulk-emit
path parses, simulates and emits every FIB in one process.

The per-topology ratios first recorded here are withdrawn (see the
2026-10-13 entry).
