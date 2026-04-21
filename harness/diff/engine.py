"""FIB diff engine — Phase 4 deliverable.

For each ``(node, vrf, prefix)`` across the union of keys from
``{vendor, batfish, hammerhead}`` the engine emits a diff record with:

- ``presence``: which sources carry this route
- ``next_hop_match``: set equality across sources that have the route
- ``protocol_match``: same protocol on both
- ``bgp_attrs_match``: AS_PATH + LOCAL_PREF + MED equal, only when both sides
  carry ``protocol == "bgp"``
"""

from __future__ import annotations


def diff_fibs(*_args, **_kwargs) -> dict:  # pragma: no cover - phase 4
    raise NotImplementedError("diff.engine.diff_fibs: phase 4")
