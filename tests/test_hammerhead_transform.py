"""Unit tests for :mod:`harness.tools.hammerhead_transform`.

The transform is pure — given a dict modelled on
``hammerhead rib --format json`` output, produce one canonical
:class:`NodeFib`. These tests lock:

- Protocol code → canonical protocol mapping (C/S/B/O/O IA/i L1/...).
- Unknown codes raise ``ValueError`` (schema drift surfaces loudly).
- Skipped codes (LDP "L", SR, EIGRP "D", ...) drop the route cleanly.
- BGP attrs populate from the nested ``bgp`` object with 0-valued MED /
  LOCAL_PREF preserved (not collapsed by an ``X or Y`` fall-through).
- ``0.0.0.0`` next-hop IP (the Rust discard marker) normalizes to None.
- Empty / missing ``entries`` array produces an empty NodeFib.
- Missing hostname raises so the orchestrator doesn't write a file with
  node name "".
- Communities merge standard + extended into one list, preserving order.
"""

from __future__ import annotations

import pytest

from harness.tools.hammerhead_transform import transform_rib_view


def _view(entries: list[dict], hostname: str = "r1") -> dict:
    return {"hostname": hostname, "entries": entries}


# --- protocol mapping -----------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("C", "connected"),
        ("S", "static"),
        ("B", "bgp"),
        ("O", "ospf"),
        ("O IA", "ospf"),
        ("O E1", "ospf"),
        ("O E2", "ospf"),
        ("i L1", "isis"),
        ("i L2", "isis"),
        ("R", "rip"),
    ],
)
def test_protocol_codes_map_to_canonical(code: str, expected: str) -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": code,
                    "admin_distance": 20,
                    "metric": 0,
                    "next_hop_interface": "eth0",
                    "next_hop_ip": "10.0.0.1",
                    "tag": 0,
                },
            ]
        )
    )
    assert len(fib.routes) == 1
    assert fib.routes[0].protocol == expected


def test_unknown_protocol_raises() -> None:
    with pytest.raises(ValueError, match="unknown Hammerhead protocol"):
        transform_rib_view(
            _view(
                [
                    {
                        "prefix": "10.0.0.0/24",
                        "protocol": "ZZZ-future",
                        "next_hop_interface": "eth0",
                    }
                ]
            )
        )


@pytest.mark.parametrize(
    "skipped_code",
    ["L", "T", "SR", "SR-TE", "SR6", "R6", "D", "D EX", "D6", "D6 EX", "Bd", "M"],
)
def test_skipped_protocols_drop_route(skipped_code: str) -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": skipped_code,
                    "next_hop_interface": "eth0",
                }
            ]
        )
    )
    assert fib.routes == []


# --- BGP attributes -------------------------------------------------------


def test_bgp_attrs_populate_from_nested_object() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "B",
                    "admin_distance": 200,
                    "metric": 0,
                    "next_hop_interface": "eth0",
                    "next_hop_ip": "10.0.12.2",
                    "tag": 0,
                    "bgp": {
                        "as_path": [65001, 65002],
                        "local_preference": 150,
                        "med": 5,
                        "origin": "igp",
                        "communities": ["65001:1", "no-export"],
                        "weight": 0,
                    },
                }
            ]
        )
    )
    r = fib.routes[0]
    assert r.as_path == [65001, 65002]
    assert r.local_pref == 150
    assert r.med == 5
    assert r.communities == ["65001:1", "no-export"]


def test_bgp_zero_med_and_local_pref_are_preserved() -> None:
    # `X or Y` would have collapsed 0 into None. This locks the fix.
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "B",
                    "next_hop_ip": "10.0.12.2",
                    "next_hop_interface": "eth0",
                    "bgp": {
                        "as_path": [65001],
                        "local_preference": 0,
                        "med": 0,
                    },
                }
            ]
        )
    )
    r = fib.routes[0]
    assert r.local_pref == 0
    assert r.med == 0


def test_bgp_communities_merge_standard_and_extended() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "B",
                    "next_hop_ip": "10.0.12.2",
                    "next_hop_interface": "eth0",
                    "bgp": {
                        "as_path": [65001],
                        "local_preference": 100,
                        "med": 0,
                        "communities": ["65001:1"],
                        "ext_communities": ["rt 65000:1"],
                    },
                }
            ]
        )
    )
    # Standard first, then extended, preserving Rust emission order.
    assert fib.routes[0].communities == ["65001:1", "rt 65000:1"]


def test_non_bgp_route_has_no_bgp_attrs() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "O",
                    "admin_distance": 110,
                    "metric": 1,
                    "next_hop_interface": "eth0",
                    "next_hop_ip": "10.0.12.2",
                }
            ]
        )
    )
    r = fib.routes[0]
    assert r.as_path is None
    assert r.local_pref is None
    assert r.med is None


# --- next-hops ------------------------------------------------------------


def test_discard_next_hop_ip_normalizes_to_none() -> None:
    # Rust emits 0.0.0.0 as the discard marker (`format_communities`
    # comment in simulate.rs, analogous behavior in rib.rs). We store
    # None on the Python side so diffs ignore the artificial address.
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "0.0.0.0/0",
                    "protocol": "S",
                    "next_hop_ip": "0.0.0.0",
                    "next_hop_interface": "null_interface",
                    "admin_distance": 1,
                }
            ]
        )
    )
    nh = fib.routes[0].next_hops[0]
    assert nh.ip is None
    assert nh.interface == "null_interface"


def test_both_null_next_hops_yields_empty_list() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.1.1.0/24",
                    "protocol": "S",
                    "admin_distance": 1,
                    # Neither field present — this is a black-hole-like
                    # entry. Canonical form is an empty next_hops list.
                }
            ]
        )
    )
    assert fib.routes[0].next_hops == []


def test_ecmp_entries_produce_multiple_routes() -> None:
    # Hammerhead emits one entry per (prefix, next-hop). Canonicalization
    # later in the diff engine merges by prefix; at transform time we
    # faithfully record one Route per entry (which is what the extract
    # schema expects too — ECMP collapses on diff, not on parse).
    view = _view(
        [
            {
                "prefix": "10.0.0.0/24",
                "protocol": "O",
                "next_hop_interface": "eth0",
                "next_hop_ip": "10.0.12.2",
            },
            {
                "prefix": "10.0.0.0/24",
                "protocol": "O",
                "next_hop_interface": "eth1",
                "next_hop_ip": "10.0.13.2",
            },
        ]
    )
    fib = transform_rib_view(view)
    assert len(fib.routes) == 2
    assert {nh.interface for r in fib.routes for nh in r.next_hops} == {"eth0", "eth1"}


# --- AS_PATH parsing ------------------------------------------------------


def test_as_path_accepts_string_form() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "B",
                    "next_hop_ip": "10.0.12.2",
                    "next_hop_interface": "eth0",
                    "bgp": {
                        "as_path": "65001 65002 65003",
                        "local_preference": 100,
                        "med": 0,
                    },
                }
            ]
        )
    )
    assert fib.routes[0].as_path == [65001, 65002, 65003]


def test_as_path_list_filters_non_integer_tokens() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "B",
                    "next_hop_ip": "10.0.12.2",
                    "next_hop_interface": "eth0",
                    "bgp": {
                        "as_path": ["65001", "ASSET", 65002, None],
                        "local_preference": 100,
                        "med": 0,
                    },
                }
            ]
        )
    )
    assert fib.routes[0].as_path == [65001, 65002]


# --- schema -------------------------------------------------------------


def test_empty_entries_list_returns_empty_nodefib() -> None:
    fib = transform_rib_view(_view([]))
    assert fib.node == "r1"
    assert fib.vrf == "default"
    assert fib.source == "hammerhead"
    assert fib.routes == []


def test_missing_entries_field_returns_empty_nodefib() -> None:
    fib = transform_rib_view({"hostname": "r1"})
    assert fib.routes == []


def test_missing_hostname_raises() -> None:
    with pytest.raises(ValueError, match="missing 'hostname'"):
        transform_rib_view({"entries": []})


def test_non_dict_entry_is_skipped() -> None:
    fib = transform_rib_view(
        {
            "hostname": "r1",
            "entries": [
                "not a dict",
                None,
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "C",
                    "next_hop_interface": "eth0",
                },
            ],
        }
    )
    assert len(fib.routes) == 1


def test_vrf_override_threads_through() -> None:
    fib = transform_rib_view(_view([]), vrf="mgmt")
    assert fib.vrf == "mgmt"


def test_vrf_global_alias_collapses_to_default() -> None:
    fib = transform_rib_view(_view([]), vrf="global")
    assert fib.vrf == "default"


def test_admin_distance_and_metric_preserved() -> None:
    fib = transform_rib_view(
        _view(
            [
                {
                    "prefix": "10.0.0.0/24",
                    "protocol": "O",
                    "admin_distance": 110,
                    "metric": 42,
                    "next_hop_interface": "eth0",
                    "next_hop_ip": "10.0.12.2",
                }
            ]
        )
    )
    r = fib.routes[0]
    assert r.admin_distance == 110
    assert r.metric == 42
