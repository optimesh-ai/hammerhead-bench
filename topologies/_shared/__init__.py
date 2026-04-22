"""Shared helpers for programmatic topology construction.

The small hand-authored topologies under ``topologies/<name>/topo.py`` are
kept literal so each one reads like a network-engineer-written fixture.
The large ones (20+, 50+, 100+ nodes) are generated programmatically by
:func:`build_spine_leaf_bgp` to avoid committing 100-node fixture files
with hand-counted /30 CIDRs — one off-by-one and the whole rig breaks.
"""
