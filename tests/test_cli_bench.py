"""Tests for the ``bench`` CLI's topology selection.

The full ``bench`` command hits Docker + pybatfish + the Hammerhead binary
— those code paths are exercised by their dedicated fakes-backed tests
(``test_pipeline_dry``, ``test_bench_hooks``, ``test_batfish``,
``test_hammerhead``). What remains for a CLI-level smoke is the pure
selection logic: ``--only``, ``--skip``, ``--max-nodes``, and the
``--with-acl-semantics`` gate.
"""

from __future__ import annotations

from harness.cli import _select_topologies


def _names(specs) -> list[str]:
    return [s.name for s in specs]


def test_default_selection_excludes_gated_topologies() -> None:
    specs = _select_topologies(
        only=set(),
        skip=set(),
        with_acl_semantics=False,
        max_nodes=None,
    )
    names = _names(specs)
    # Phase 7 ships 10 topologies; acl-semantics-3node is gated.
    assert "acl-semantics-3node" not in names
    # Sanity: every non-gated topology that actually has a topo.py loads.
    for expected in (
        "bgp-ebgp-2node",
        "bgp-ibgp-2node",
        "ospf-broadcast-4node",
        "ospf-p2p-3node",
        "isis-l1l2-4node",
        "mpls-l3vpn-4node",
        "route-reflector-6node",
        "spine-leaf-6node",
        "route-map-pathological",
        "acl-heavy-parse",
    ):
        assert expected in names, f"{expected} missing from default selection"


def test_only_restricts_to_named_topologies() -> None:
    specs = _select_topologies(
        only={"bgp-ibgp-2node", "ospf-p2p-3node"},
        skip=set(),
        with_acl_semantics=False,
        max_nodes=None,
    )
    assert _names(specs) == ["bgp-ibgp-2node", "ospf-p2p-3node"]


def test_skip_drops_named_topologies() -> None:
    specs = _select_topologies(
        only=set(),
        skip={"bgp-ibgp-2node"},
        with_acl_semantics=False,
        max_nodes=None,
    )
    assert "bgp-ibgp-2node" not in _names(specs)


def test_max_nodes_filters_larger_topologies() -> None:
    specs = _select_topologies(
        only=set(),
        skip=set(),
        with_acl_semantics=False,
        max_nodes=3,
    )
    # bgp-ibgp-2node (2 nodes) stays; route-reflector-6node (7 nodes
    # including hub) goes.
    names = _names(specs)
    assert "bgp-ibgp-2node" in names
    assert "route-reflector-6node" not in names
    assert "spine-leaf-6node" not in names


def test_acl_semantics_gate_loads_mixed_vendor_topology_when_enabled() -> None:
    # Phase 8: the acl-semantics-3node SPEC is real (r1/r3 FRR + r2 cEOS).
    # The gate is working iff --with-acl-semantics=True surfaces it in the
    # select set and --with-acl-semantics=False hides it.
    specs = _select_topologies(
        only={"acl-semantics-3node"},
        skip=set(),
        with_acl_semantics=True,
        max_nodes=None,
    )
    assert "acl-semantics-3node" in _names(specs)

    hidden = _select_topologies(
        only=set(),
        skip=set(),
        with_acl_semantics=False,
        max_nodes=None,
    )
    assert "acl-semantics-3node" not in _names(hidden)
