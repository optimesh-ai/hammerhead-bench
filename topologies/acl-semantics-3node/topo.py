"""acl-semantics-3node — flow-audit diff across FRR + cEOS + Batfish + Hammerhead.

**Phase 7 stub.** Importing this module raises :class:`NotImplementedError`
immediately so accidental inclusion in a bench run (via
``--with-acl-semantics`` before phase 8 ships) fails loud instead of
silently rendering a half-working topology.

Phase 8 will replace this body with the real spec: a 3-router triangle
where r2 is Arista cEOS carrying a curated overlapping ACL. The benchmark
is a per-probe permit/deny diff across vendor truth (cEOS ``show
access-lists``), Batfish ``testFilters``, and Hammerhead ``acl-audit``.
"""

from __future__ import annotations

raise NotImplementedError(
    "acl-semantics-3node is a phase-7 stub; the cEOS adapter + topology "
    "body land in phase 8. Do not use --with-acl-semantics until then."
)

# Phase 8 will populate SPEC below; keeping the symbol declared so static
# import tooling (mypy, ruff) doesn't complain when this file becomes real.
SPEC = None  # type: ignore[assignment]
