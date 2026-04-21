"""FIB normalization tests.

Phase 1 ships the smoke tests for VRF alias + next-hop sort so the canonical
schema is locked in before adapters start producing output. The "10+ cases"
spec target is expanded in phase 4 (diff engine) and phase 7 (real topology
output feeding in).
"""

from __future__ import annotations

import pydantic
import pytest

from harness.extract.fib import (
    NextHop,
    NodeFib,
    Route,
    canonicalize_next_hops,
    canonicalize_node_fib,
    canonicalize_vrf,
)


def test_vrf_empty_becomes_default() -> None:
    assert canonicalize_vrf("") == "default"


def test_vrf_global_becomes_default() -> None:
    assert canonicalize_vrf("global") == "default"


def test_vrf_master_becomes_default() -> None:
    assert canonicalize_vrf("master") == "default"


def test_vrf_uppercase_alias_still_normalized() -> None:
    assert canonicalize_vrf("Global") == "default"


def test_vrf_real_name_preserved() -> None:
    assert canonicalize_vrf("CUSTOMER_RED") == "CUSTOMER_RED"


def test_next_hops_sorted_by_ip_then_interface() -> None:
    nhs = [
        NextHop(ip="10.0.0.3", interface=None),
        NextHop(ip=None, interface="Ethernet1"),
        NextHop(ip="10.0.0.1", interface="Ethernet0"),
    ]
    sorted_nhs = canonicalize_next_hops(nhs)
    assert [n.ip for n in sorted_nhs] == [None, "10.0.0.1", "10.0.0.3"]


def test_canonical_node_fib_sorts_routes_by_prefix() -> None:
    fib = NodeFib(
        node="r1",
        vrf="",  # -> default
        source="vendor",
        routes=[
            Route(prefix="10.0.2.0/24", protocol="ospf"),
            Route(prefix="10.0.1.0/24", protocol="ospf"),
            Route(prefix="10.0.1.0/24", protocol="bgp"),
        ],
    )
    canonical = canonicalize_node_fib(fib)
    assert canonical.vrf == "default"
    assert [r.prefix for r in canonical.routes] == [
        "10.0.1.0/24",
        "10.0.1.0/24",
        "10.0.2.0/24",
    ]
    # Within same prefix, protocol asc = "bgp" before "ospf".
    assert canonical.routes[0].protocol == "bgp"


def test_loopback_filter_off_by_default_keeps_host_routes() -> None:
    fib = NodeFib(
        node="r1",
        vrf="default",
        source="vendor",
        routes=[
            Route(
                prefix="1.1.1.1/32",
                protocol="connected",
                next_hops=[NextHop(interface="Loopback0")],
            ),
        ],
    )
    kept = canonicalize_node_fib(fib)
    assert len(kept.routes) == 1


def test_loopback_filter_on_strips_connected_host_route() -> None:
    fib = NodeFib(
        node="r1",
        vrf="default",
        source="vendor",
        routes=[
            Route(
                prefix="1.1.1.1/32",
                protocol="connected",
                next_hops=[NextHop(interface="Loopback0")],
            ),
            Route(
                prefix="10.0.0.0/24",
                protocol="connected",
                next_hops=[NextHop(interface="Ethernet0")],
            ),
        ],
    )
    filtered = canonicalize_node_fib(fib, filter_loopback_host=True)
    assert [r.prefix for r in filtered.routes] == ["10.0.0.0/24"]


def test_loopback_filter_keeps_static_host_route() -> None:
    fib = NodeFib(
        node="r1",
        vrf="default",
        source="vendor",
        routes=[
            Route(
                prefix="8.8.8.8/32",
                protocol="static",
                next_hops=[NextHop(ip="10.0.0.1")],
            ),
        ],
    )
    filtered = canonicalize_node_fib(fib, filter_loopback_host=True)
    assert len(filtered.routes) == 1


def test_extra_fields_rejected() -> None:
    # pydantic extra="forbid" — any schema drift in tool output raises.
    with pytest.raises(pydantic.ValidationError):
        NodeFib.model_validate(
            {
                "node": "r1",
                "vrf": "default",
                "source": "vendor",
                "routes": [],
                "surprise": "should fail",
            }
        )
