"""cEOS adapter tests — render-clab, EOS JSON parsing, convergence helpers.

The live ``docker exec ... Cli`` path is covered structurally by the pipeline
integration tests (``test_pipeline_dry`` swaps in a fake lab); this module
owns the adapter's pure surface: clab-YAML shape, EOS route-JSON parser,
BGP-attribute flattener, and the convergence-detection predicates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.adapters.ceos import (
    CEOS_DEFAULT_IMAGE,
    CEOS_DEFAULT_MEMORY_MB,
    CeosAdapter,
    _all_bgp_sessions_established,
    _flatten_eos_bgp,
    _total_route_count,
)
from harness.extract.fib import merge_bgp_attributes, parse_eos_route_json

# --- render_clab_node -------------------------------------------------------


def test_render_clab_node_emits_ceos_kind_with_startup_config() -> None:
    adapter = CeosAdapter()
    out = adapter.render_clab_node("r2", Path("configs/r2"))
    assert out["kind"] == "ceos"
    assert out["image"] == CEOS_DEFAULT_IMAGE
    assert out["memory"] == f"{CEOS_DEFAULT_MEMORY_MB}m"
    assert out["startup-config"] == "configs/r2/startup-config"


def test_env_var_overrides_default_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEOS_IMAGE", "ceos:4.33.0F-custom")
    adapter = CeosAdapter()
    assert adapter.image == "ceos:4.33.0F-custom"


def test_explicit_image_wins_over_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEOS_IMAGE", "ceos:should-lose")
    adapter = CeosAdapter(image="ceos:explicit-wins")
    assert adapter.image == "ceos:explicit-wins"


def test_config_template_names_is_startup_config_only() -> None:
    adapter = CeosAdapter()
    # Mixed-vendor topologies rely on this allow-list so an FRR node doesn't
    # accidentally get a startup-config rendered and vice versa.
    assert adapter.config_template_names == ("startup-config.j2",)


# --- EOS route JSON parsing -------------------------------------------------


_EOS_ROUTE_SAMPLE = {
    "vrfs": {
        "default": {
            "routes": {
                "10.0.0.1/32": {
                    "kernelProgrammed": True,
                    "routeAction": "forward",
                    "protocol": "ospf intra area",
                    "preference": 110,
                    "metric": 20,
                    "vias": [{"interface": "Ethernet1", "nexthopAddr": "10.0.12.1"}],
                },
                "10.0.12.0/30": {
                    "kernelProgrammed": True,
                    "routeAction": "forward",
                    "protocol": "connected",
                    "preference": 0,
                    "metric": 0,
                    "vias": [{"interface": "Ethernet1", "nexthopAddr": "0.0.0.0"}],
                },
                "10.0.99.0/24": {
                    "kernelProgrammed": True,
                    "routeAction": "forward",
                    "protocol": "bgp",
                    "preference": 200,
                    "metric": 0,
                    "vias": [{"interface": "Ethernet2", "nexthopAddr": "10.0.23.2"}],
                },
                "192.168.50.0/24": {
                    # routeAction=drop must be skipped (black-hole entry).
                    "kernelProgrammed": True,
                    "routeAction": "drop",
                    "protocol": "static",
                    "preference": 1,
                    "metric": 0,
                    "vias": [],
                },
                "172.31.0.0/16": {
                    # kernelProgrammed=False must be skipped (not in FIB yet).
                    "kernelProgrammed": False,
                    "routeAction": "forward",
                    "protocol": "bgp",
                    "preference": 200,
                    "metric": 0,
                    "vias": [{"nexthopAddr": "10.0.12.1"}],
                },
            },
        },
        "CUSTOMER_RED": {
            "routes": {
                "192.168.10.0/24": {
                    "kernelProgrammed": True,
                    "routeAction": "forward",
                    "protocol": "bgp",
                    "preference": 200,
                    "metric": 0,
                    "vias": [{"interface": "Ethernet3", "nexthopAddr": "10.1.0.1"}],
                }
            }
        },
    }
}


def test_parse_eos_route_json_emits_one_fib_per_vrf() -> None:
    fibs = parse_eos_route_json(_EOS_ROUTE_SAMPLE, node_name="r2")
    vrfs = sorted(f.vrf for f in fibs)
    assert vrfs == ["CUSTOMER_RED", "default"]


def test_parse_eos_route_json_drops_non_forward_and_non_programmed() -> None:
    fibs = parse_eos_route_json(_EOS_ROUTE_SAMPLE, node_name="r2")
    default = next(f for f in fibs if f.vrf == "default")
    prefixes = [r.prefix for r in default.routes]
    assert "192.168.50.0/24" not in prefixes  # routeAction=drop
    assert "172.31.0.0/16" not in prefixes  # kernelProgrammed=False


def test_parse_eos_route_json_maps_ospf_subprotocols_to_parent() -> None:
    fibs = parse_eos_route_json(_EOS_ROUTE_SAMPLE, node_name="r2")
    default = next(f for f in fibs if f.vrf == "default")
    by_prefix = {r.prefix: r for r in default.routes}
    assert by_prefix["10.0.0.1/32"].protocol == "ospf"
    assert by_prefix["10.0.0.1/32"].admin_distance == 110
    assert by_prefix["10.0.0.1/32"].metric == 20


def test_parse_eos_route_json_normalizes_connected_next_hop_zero() -> None:
    fibs = parse_eos_route_json(_EOS_ROUTE_SAMPLE, node_name="r2")
    default = next(f for f in fibs if f.vrf == "default")
    by_prefix = {r.prefix: r for r in default.routes}
    connected = by_prefix["10.0.12.0/30"]
    assert connected.protocol == "connected"
    # 0.0.0.0 should be stripped — FRR represents connected routes as
    # interface-only, so we match that representation.
    assert connected.next_hops[0].ip is None
    assert connected.next_hops[0].interface == "Ethernet1"


def test_parse_eos_route_json_empty_returns_default_empty_fib() -> None:
    fibs = parse_eos_route_json({}, node_name="r2")
    assert len(fibs) == 1
    assert fibs[0].vrf == "default"
    assert fibs[0].routes == []


def test_parse_eos_route_json_empty_vrfs_block_still_emits_default() -> None:
    fibs = parse_eos_route_json({"vrfs": {}}, node_name="r2")
    assert [f.vrf for f in fibs] == ["default"]
    assert fibs[0].routes == []


def test_parse_eos_route_json_rejects_unknown_protocol() -> None:
    bad = {
        "vrfs": {
            "default": {
                "routes": {
                    "10.0.0.0/24": {
                        "kernelProgrammed": True,
                        "routeAction": "forward",
                        "protocol": "unexpected-new-proto",
                        "preference": 0,
                        "metric": 0,
                        "vias": [{"nexthopAddr": "10.0.0.1"}],
                    }
                }
            }
        }
    }
    with pytest.raises(ValueError, match="unknown EOS protocol"):
        parse_eos_route_json(bad, node_name="r2")


def test_parse_eos_route_json_falls_back_to_route_type_when_protocol_missing() -> None:
    # Some EOS versions omit ``protocol`` and only emit ``routeType``.
    data = {
        "vrfs": {
            "default": {
                "routes": {
                    "10.0.0.1/32": {
                        "kernelProgrammed": True,
                        "routeAction": "forward",
                        "routeType": "ospfInterArea",
                        "preference": 110,
                        "metric": 5,
                        "vias": [{"interface": "Ethernet1", "nexthopAddr": "10.0.12.1"}],
                    }
                }
            }
        }
    }
    fibs = parse_eos_route_json(data, node_name="r2")
    default = next(f for f in fibs if f.vrf == "default")
    assert default.routes[0].protocol == "ospf"


# --- BGP attribute flattening -----------------------------------------------


def test_flatten_eos_bgp_converts_to_frr_compatible_shape() -> None:
    eos = {
        "vrfs": {
            "default": {
                "bgpRouteEntries": {
                    "10.0.99.0/24": {
                        "bgpRoutePaths": [
                            {
                                "asPathEntry": {"asPath": "65100 65200"},
                                "localPreference": 130,
                                "med": 0,
                                "reasonNotBestpath": None,
                            },
                            {
                                "asPathEntry": {"asPath": "65300 65400"},
                                "localPreference": 100,
                                "med": 50,
                                "reasonNotBestpath": "higherLocalPref",
                            },
                        ]
                    }
                }
            }
        }
    }
    flat = _flatten_eos_bgp(eos)
    route_entry = flat["default"]["routes"]["10.0.99.0/24"]
    assert len(route_entry) == 2
    best = next(p for p in route_entry if p["bestpath"])
    assert best["path"] == "65100 65200"
    assert best["locPrf"] == 130


def test_merge_bgp_attributes_populates_locpref_on_eos_route() -> None:
    fibs = parse_eos_route_json(_EOS_ROUTE_SAMPLE, node_name="r2")
    default = next(f for f in fibs if f.vrf == "default")
    eos_bgp = {
        "vrfs": {
            "default": {
                "bgpRouteEntries": {
                    "10.0.99.0/24": {
                        "bgpRoutePaths": [
                            {
                                "asPathEntry": {"asPath": "65100 65200"},
                                "localPreference": 130,
                                "med": 5,
                                "reasonNotBestpath": None,
                            }
                        ]
                    }
                }
            }
        }
    }
    flat = _flatten_eos_bgp(eos_bgp)
    merged = merge_bgp_attributes(default, flat["default"])
    by_prefix = {r.prefix: r for r in merged.routes}
    bgp = by_prefix["10.0.99.0/24"]
    assert bgp.local_pref == 130
    assert bgp.med == 5
    assert bgp.as_path == [65100, 65200]
    # Connected + OSPF routes are passed through untouched.
    assert by_prefix["10.0.12.0/30"].local_pref is None


# --- convergence helpers ----------------------------------------------------


def test_all_bgp_sessions_established_true_when_every_peer_up() -> None:
    data = json.dumps(
        {
            "vrfs": {
                "default": {"peers": {"10.0.12.1": {"peerState": "Established"}}},
                "CUSTOMER_RED": {"peers": {"10.1.0.1": {"peerState": "Established"}}},
            }
        }
    )
    assert _all_bgp_sessions_established(data) is True


def test_all_bgp_sessions_established_false_when_any_peer_down() -> None:
    data = json.dumps(
        {
            "vrfs": {
                "default": {
                    "peers": {
                        "10.0.12.1": {"peerState": "Established"},
                        "10.0.12.5": {"peerState": "Active"},
                    }
                }
            }
        }
    )
    assert _all_bgp_sessions_established(data) is False


def test_all_bgp_sessions_established_accepts_bgpstate_key_name() -> None:
    # EOS's ``show ip bgp summary vrf all | json`` in some versions emits
    # ``bgpState`` instead of ``peerState``. Handle both.
    data = json.dumps({"vrfs": {"default": {"peers": {"10.0.12.1": {"bgpState": "Established"}}}}})
    assert _all_bgp_sessions_established(data) is True


def test_all_bgp_sessions_established_no_bgp_configured_is_trivially_true() -> None:
    # A pure-OSPF topology has no BGP. That's a trivially converged case.
    assert _all_bgp_sessions_established("{}") is True
    assert _all_bgp_sessions_established('{"vrfs": {}}') is True


def test_total_route_count_sums_across_vrfs() -> None:
    data = json.dumps(_EOS_ROUTE_SAMPLE)
    # 5 in default + 1 in CUSTOMER_RED = 6 (includes non-forward entries; the
    # count is for convergence stability not for the canonical FIB).
    assert _total_route_count(data) == 6


def test_total_route_count_empty_returns_zero() -> None:
    assert _total_route_count("") == 0
    assert _total_route_count("{}") == 0
