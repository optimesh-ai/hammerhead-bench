"""FRR JSON parsing tests.

Fixtures under ``tests/fixtures/frr/`` capture the literal output of
``vtysh -c 'show ip route vrf all json'`` and ``show ip bgp vrf all json``
from a real FRR 8.4.1 container running the bgp-ibgp-2node topology. If FRR
changes the output shape, these tests go red and we update the parser + the
fixture in the same commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.extract.fib import (
    merge_bgp_attributes,
    parse_frr_route_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "frr"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_route_json_emits_one_fib_per_vrf() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    assert len(fibs) == 1
    assert fibs[0].node == "r1"
    assert fibs[0].vrf == "default"
    assert fibs[0].source == "vendor"


def test_parse_route_json_filters_non_installed_and_kernel() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    prefixes = [r.prefix for r in fibs[0].routes]
    # kernel (169.254.0.0/16) is dropped; the static/non-installed 10.0.0.2 entry
    # is dropped because selected+installed filters to the BGP copy only.
    assert "169.254.0.0/16" not in prefixes
    assert prefixes.count("10.0.0.2/32") == 1
    proto_of = {r.prefix: r.protocol for r in fibs[0].routes}
    assert proto_of["10.0.0.2/32"] == "bgp"


def test_parse_route_json_preserves_distance_and_metric() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    by_prefix = {r.prefix: r for r in fibs[0].routes}
    bgp = by_prefix["10.0.0.2/32"]
    assert bgp.admin_distance == 200
    assert bgp.metric == 0
    assert len(bgp.next_hops) == 1
    assert bgp.next_hops[0].ip == "10.0.12.2"
    assert bgp.next_hops[0].interface == "eth1"


def test_parse_route_json_canonicalizes_multi_vrf() -> None:
    fibs = parse_frr_route_json(_load("multi_vrf_route.json"), node_name="pe1")
    vrfs = sorted(f.vrf for f in fibs)
    assert vrfs == ["CUSTOMER_RED", "default"]
    red = next(f for f in fibs if f.vrf == "CUSTOMER_RED")
    assert [r.prefix for r in red.routes] == ["192.168.10.0/24"]
    assert red.routes[0].protocol == "bgp"


def test_parse_route_json_empty_dict_returns_default_vrf_empty_fib() -> None:
    fibs = parse_frr_route_json({}, node_name="r1")
    assert len(fibs) == 1
    assert fibs[0].vrf == "default"
    assert fibs[0].routes == []


def test_parse_route_json_rejects_unknown_protocol() -> None:
    bad = {
        "default": {
            "10.0.0.0/24": [
                {
                    "protocol": "UNEXPECTED_NEW_PROTOCOL",
                    "installed": True,
                    "selected": True,
                    "distance": 0,
                    "metric": 0,
                    "nexthops": [{"ip": "10.0.0.1"}],
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="unknown FRR protocol"):
        parse_frr_route_json(bad, node_name="r1")


def test_merge_bgp_attributes_populates_locpref_on_bgp_routes_only() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    merged = merge_bgp_attributes(fibs[0], _load("r1_show_ip_bgp.json"))
    by_prefix = {r.prefix: r for r in merged.routes}

    bgp = by_prefix["10.0.0.2/32"]
    assert bgp.local_pref == 100
    assert bgp.med == 0
    assert bgp.as_path == []  # empty iBGP path

    connected = by_prefix["10.0.12.0/30"]
    assert connected.local_pref is None
    assert connected.med is None
    assert connected.as_path is None


def test_merge_bgp_attributes_leaves_unmatched_bgp_routes_untouched() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    # Empty BGP dict: no matches, BGP route passes through.
    merged = merge_bgp_attributes(fibs[0], {})
    by_prefix = {r.prefix: r for r in merged.routes}
    assert by_prefix["10.0.0.2/32"].local_pref is None


def test_merge_bgp_attributes_parses_as_path_ints() -> None:
    fibs = parse_frr_route_json(_load("r1_show_ip_route.json"), node_name="r1")
    bgp_json = _load("r1_show_ip_bgp.json")
    # Simulate an eBGP path for the same prefix.
    bgp_json["routes"]["10.0.0.2/32"][0]["path"] = "65200 65300"
    merged = merge_bgp_attributes(fibs[0], bgp_json)
    by_prefix = {r.prefix: r for r in merged.routes}
    assert by_prefix["10.0.0.2/32"].as_path == [65200, 65300]
