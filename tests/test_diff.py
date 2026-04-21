"""Diff engine + metrics tests — Phase 4."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.diff.engine import (
    DiffRecord,
    DiffWorkspace,
    diff_fibs,
    load_fib_workspace,
)
from harness.diff.metrics import aggregate, aggregate_many
from harness.extract.fib import NextHop, NodeFib, Route

# ---- fixtures ------------------------------------------------------------


def _route(prefix: str, protocol: str, *nhs: tuple[str | None, str | None], **kw) -> Route:
    return Route(
        prefix=prefix,
        protocol=protocol,  # type: ignore[arg-type]
        next_hops=[NextHop(ip=ip, interface=iface) for ip, iface in nhs],
        **kw,
    )


def _fib(node: str, source: str, *routes: Route, vrf: str = "default") -> NodeFib:
    return NodeFib(
        node=node, vrf=vrf, source=source, routes=list(routes)  # type: ignore[arg-type]
    )


# ---- core diff -----------------------------------------------------------


def test_diff_all_three_agree_produces_all_three_presence() -> None:
    r = _route("10.0.0.1/32", "connected", (None, "lo"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r)],
        batfish=[_fib("r1", "batfish", r)],
        hammerhead=[_fib("r1", "hammerhead", r)],
    )
    out = diff_fibs(ws)
    assert len(out) == 1
    rec = out[0]
    assert rec.presence == "all-three"
    assert rec.batfish_next_hop_match is True
    assert rec.hammerhead_next_hop_match is True
    assert rec.batfish_protocol_match is True
    assert rec.hammerhead_protocol_match is True
    # Non-BGP => bgp_attrs_match is None.
    assert rec.batfish_bgp_attrs_match is None
    assert rec.hammerhead_bgp_attrs_match is None


def test_diff_vendor_only_marks_simulators_missing() -> None:
    r = _route("10.1.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(vendor=[_fib("r1", "vendor", r)])
    out = diff_fibs(ws)
    assert len(out) == 1
    assert out[0].presence == "vendor-only"
    # No comparison possible => match bits stay None.
    assert out[0].batfish_next_hop_match is None
    assert out[0].hammerhead_next_hop_match is None


def test_diff_vendor_and_hammerhead_only_sets_hammerhead_match_bits() -> None:
    r = _route("10.2.0.0/24", "static", ("10.0.12.2", None))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r)],
        hammerhead=[_fib("r1", "hammerhead", r)],
    )
    out = diff_fibs(ws)
    assert out[0].presence == "vendor-and-hammerhead"
    assert out[0].hammerhead_next_hop_match is True
    assert out[0].hammerhead_protocol_match is True
    assert out[0].batfish_next_hop_match is None
    assert out[0].batfish_protocol_match is None


def test_diff_next_hop_order_is_ignored_via_canonicalization() -> None:
    # ECMP: vendor reports A then B, batfish reports B then A. Canonicalization
    # sorts both lexicographically so the sets compare equal.
    vendor = _route("10.3.0.0/24", "ospf", ("10.0.12.2", "eth1"), ("10.0.13.3", "eth2"))
    batfish = _route("10.3.0.0/24", "ospf", ("10.0.13.3", "eth2"), ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", vendor)],
        batfish=[_fib("r1", "batfish", batfish)],
    )
    out = diff_fibs(ws)
    assert out[0].presence == "vendor-and-batfish"
    assert out[0].batfish_next_hop_match is True


def test_diff_different_next_hops_marks_mismatch() -> None:
    vendor = _route("10.4.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    batfish = _route("10.4.0.0/24", "ospf", ("10.0.99.99", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", vendor)],
        batfish=[_fib("r1", "batfish", batfish)],
    )
    out = diff_fibs(ws)
    assert out[0].batfish_next_hop_match is False
    assert out[0].batfish_protocol_match is True  # still same protocol


def test_diff_different_protocol_marks_protocol_mismatch_and_bgp_attrs_none() -> None:
    vendor = _route("10.5.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    # Hammerhead thinks it's BGP. Protocol mismatches => bgp_attrs not applicable.
    hammerhead = _route(
        "10.5.0.0/24", "bgp", ("10.0.12.2", "eth1"), as_path=[65001], local_pref=100, med=0
    )
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", vendor)],
        hammerhead=[_fib("r1", "hammerhead", hammerhead)],
    )
    out = diff_fibs(ws)
    assert out[0].hammerhead_protocol_match is False
    # bgp_attrs_match is only set when both sides are BGP.
    assert out[0].hammerhead_bgp_attrs_match is None


def test_diff_bgp_attrs_equal_and_unequal() -> None:
    v = _route(
        "10.6.0.0/24", "bgp", ("10.0.12.2", None),
        as_path=[65001, 65002], local_pref=200, med=50,
    )
    b_same = _route(
        "10.6.0.0/24", "bgp", ("10.0.12.2", None),
        as_path=[65001, 65002], local_pref=200, med=50,
    )
    b_diff_aspath = _route(
        "10.6.0.0/24", "bgp", ("10.0.12.2", None),
        as_path=[65001, 65003], local_pref=200, med=50,
    )
    b_diff_lp = _route(
        "10.6.0.0/24", "bgp", ("10.0.12.2", None),
        as_path=[65001, 65002], local_pref=100, med=50,
    )
    b_diff_med = _route(
        "10.6.0.0/24", "bgp", ("10.0.12.2", None),
        as_path=[65001, 65002], local_pref=200, med=10,
    )

    for bf, expected in [(b_same, True), (b_diff_aspath, False),
                          (b_diff_lp, False), (b_diff_med, False)]:
        ws = DiffWorkspace(
            vendor=[_fib("r1", "vendor", v)],
            batfish=[_fib("r1", "batfish", bf)],
        )
        out = diff_fibs(ws)
        assert out[0].batfish_bgp_attrs_match is expected, f"bf={bf}"


def test_diff_vrf_aliases_collapse_to_default() -> None:
    r = _route("10.7.0.0/24", "connected", (None, "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r, vrf="")],
        batfish=[_fib("r1", "batfish", r, vrf="global")],
    )
    out = diff_fibs(ws)
    # Both collapsed to "default"; the two routes merge into one key.
    assert len(out) == 1
    assert out[0].vrf == "default"


def test_diff_sorted_output_is_deterministic() -> None:
    r1 = _route("10.0.0.0/24", "connected", (None, "eth1"))
    r2 = _route("10.0.1.0/24", "connected", (None, "eth2"))
    ws = DiffWorkspace(
        vendor=[_fib("r2", "vendor", r2), _fib("r1", "vendor", r1)],
        batfish=[_fib("r1", "batfish", r1), _fib("r2", "batfish", r2)],
    )
    out = diff_fibs(ws)
    keys = [(r.node, r.vrf, r.prefix) for r in out]
    assert keys == sorted(keys)


def test_diff_record_as_dict_is_json_serializable() -> None:
    r = _route("10.8.0.0/24", "static", ("10.0.12.2", None))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r)],
        batfish=[_fib("r1", "batfish", r)],
    )
    [rec] = diff_fibs(ws)
    payload = rec.as_dict()
    # Round-trip through JSON to confirm no tuples leak through.
    serialized = json.dumps(payload)
    restored = json.loads(serialized)
    assert restored["node"] == "r1"
    assert restored["presence"] == "vendor-and-batfish"
    assert restored["vendor_next_hops"] == [["10.0.12.2", None]]


# ---- metrics -------------------------------------------------------------


def test_metrics_empty_workspace_returns_1_0_rates() -> None:
    m = aggregate("empty-topo", [])
    assert m.topology == "empty-topo"
    assert m.total_routes_vendor == 0
    assert m.batfish_next_hop_match_rate == 1.0  # _safe_div fallback
    assert m.hammerhead_next_hop_match_rate == 1.0


def test_metrics_single_perfect_match() -> None:
    r = _route("10.0.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r)],
        batfish=[_fib("r1", "batfish", r)],
        hammerhead=[_fib("r1", "hammerhead", r)],
    )
    m = aggregate("perfect", diff_fibs(ws))
    assert m.total_routes_vendor == 1
    assert m.batfish_presence_match_rate == 1.0
    assert m.batfish_next_hop_match_rate == 1.0
    assert m.batfish_protocol_match_rate == 1.0
    # No BGP routes => bgp_attr_match_rate defaults to 1.0 (safe-div of 0/0).
    assert m.batfish_bgp_attr_match_rate == 1.0
    assert m.batfish_per_protocol_next_hop_match_rate == {"ospf": 1.0}


def test_metrics_mixed_next_hop_and_protocol_mismatch() -> None:
    # Two routes: one perfect, one next-hop diff on batfish only.
    good = _route("10.0.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    bad_hh = _route("10.1.0.0/24", "ospf", ("10.0.99.99", "eth1"))
    bad_bf = _route("10.1.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", good, bad_bf)],
        batfish=[_fib("r1", "batfish", good, bad_bf)],
        hammerhead=[_fib("r1", "hammerhead", good, bad_hh)],
    )
    m = aggregate("mixed", diff_fibs(ws))
    assert m.batfish_next_hop_match_rate == 1.0  # both agree with vendor
    assert m.hammerhead_next_hop_match_rate == 0.5  # one of two matches


def test_metrics_bgp_attr_rate_only_counts_both_bgp_rows() -> None:
    v_bgp = _route("10.0.0.0/24", "bgp", ("10.0.12.2", None),
                   as_path=[65001], local_pref=100, med=0)
    v_ospf = _route("10.1.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    b_bgp_diff = _route("10.0.0.0/24", "bgp", ("10.0.12.2", None),
                        as_path=[65001, 65002], local_pref=100, med=0)
    b_ospf = _route("10.1.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", v_bgp, v_ospf)],
        batfish=[_fib("r1", "batfish", b_bgp_diff, b_ospf)],
    )
    m = aggregate("bgp-attr", diff_fibs(ws))
    # 1 BGP row, it mismatches => 0/1 = 0.0. OSPF rows don't factor in.
    assert m.batfish_bgp_attr_match_rate == 0.0


def test_metrics_presence_rate_counts_only_both_sides() -> None:
    r_both = _route("10.0.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    r_vendor_only = _route("10.1.0.0/24", "static", ("10.0.12.2", None))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r_both, r_vendor_only)],
        batfish=[_fib("r1", "batfish", r_both)],
    )
    m = aggregate("presence", diff_fibs(ws))
    # 2 vendor routes, 1 also in batfish => 50%.
    assert m.batfish_presence_match_rate == 0.5


def test_aggregate_many_empty_returns_unit_rates() -> None:
    out = aggregate_many([])
    assert out == {
        "topology_count": 0,
        "batfish_next_hop_match_rate_mean": 1.0,
        "hammerhead_next_hop_match_rate_mean": 1.0,
    }


def test_aggregate_many_two_topologies_averages_correctly() -> None:
    good = _route("10.0.0.0/24", "ospf", ("10.0.12.2", "eth1"))
    bad_hh = _route("10.0.0.0/24", "ospf", ("10.0.99.99", "eth1"))

    ws1 = DiffWorkspace(
        vendor=[_fib("r1", "vendor", good)],
        hammerhead=[_fib("r1", "hammerhead", good)],
    )
    ws2 = DiffWorkspace(
        vendor=[_fib("r1", "vendor", good)],
        hammerhead=[_fib("r1", "hammerhead", bad_hh)],
    )
    m1 = aggregate("topo1", diff_fibs(ws1))  # 1.0 nh match rate
    m2 = aggregate("topo2", diff_fibs(ws2))  # 0.0 nh match rate
    agg = aggregate_many([m1, m2])
    assert agg["topology_count"] == 2
    assert agg["hammerhead_next_hop_match_rate_mean"] == 0.5


# ---- workspace loader ----------------------------------------------------


def test_load_fib_workspace_reads_all_three_subdirs(tmp_path: Path) -> None:
    topo = "bgp-ibgp-2node"
    root = tmp_path / "results"
    r = _route("10.0.0.1/32", "connected", (None, "lo"))
    vendor_dir = root / "vendor_truth" / topo
    batfish_dir = root / "batfish" / topo
    ham_dir = root / "hammerhead" / topo
    vendor_dir.mkdir(parents=True)
    batfish_dir.mkdir(parents=True)
    ham_dir.mkdir(parents=True)
    (vendor_dir / "r1__default.json").write_text(
        _fib("r1", "vendor", r).model_dump_json()
    )
    (batfish_dir / "r1__default.json").write_text(
        _fib("r1", "batfish", r).model_dump_json()
    )
    (ham_dir / "r1__default.json").write_text(
        _fib("r1", "hammerhead", r).model_dump_json()
    )
    ws = load_fib_workspace(root, topo)
    assert [f.source for f in ws.vendor] == ["vendor"]
    assert [f.source for f in ws.batfish] == ["batfish"]
    assert [f.source for f in ws.hammerhead] == ["hammerhead"]


def test_load_fib_workspace_handles_missing_sources(tmp_path: Path) -> None:
    topo = "bgp-ibgp-2node"
    root = tmp_path / "results"
    r = _route("10.0.0.1/32", "connected", (None, "lo"))
    vendor_dir = root / "vendor_truth" / topo
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "r1__default.json").write_text(_fib("r1", "vendor", r).model_dump_json())
    # Only vendor exists; batfish + hammerhead dirs absent.
    ws = load_fib_workspace(root, topo)
    assert len(ws.vendor) == 1
    assert ws.batfish == []
    assert ws.hammerhead == []
    # And diff still works — everything becomes vendor-only.
    [rec] = diff_fibs(ws)
    assert rec.presence == "vendor-only"


# ---- scorecard cross-check -----------------------------------------------


def test_diffrecord_is_dataclass_with_expected_fields() -> None:
    # Defensive: if someone adds a new per-sim bit, they must wire it into
    # metrics._collect too. This test fails loudly when DiffRecord grows.
    expected = {
        "node", "vrf", "prefix", "presence",
        "vendor_protocol", "batfish_protocol", "hammerhead_protocol",
        "vendor_next_hops", "batfish_next_hops", "hammerhead_next_hops",
        "batfish_next_hop_match", "hammerhead_next_hop_match",
        "batfish_protocol_match", "hammerhead_protocol_match",
        "batfish_bgp_attrs_match", "hammerhead_bgp_attrs_match",
    }
    actual = set(DiffRecord.__dataclass_fields__.keys())
    assert actual == expected, "DiffRecord shape changed; update metrics._collect"


# Smoke test that the old stub is gone (prevents accidental revert).
def test_diff_engine_is_wired() -> None:
    out = diff_fibs(DiffWorkspace())
    assert out == []  # empty workspace => empty diff, not NotImplementedError


@pytest.mark.parametrize("proto", ["connected", "static", "ospf", "bgp"])
def test_diff_each_protocol_produces_per_protocol_breakdown(proto: str) -> None:
    r = _route("10.9.0.0/24", proto, ("10.0.12.2", "eth1"))
    ws = DiffWorkspace(
        vendor=[_fib("r1", "vendor", r)],
        batfish=[_fib("r1", "batfish", r)],
    )
    m = aggregate("proto-test", diff_fibs(ws))
    assert proto in m.batfish_per_protocol_next_hop_match_rate
    assert m.batfish_per_protocol_next_hop_match_rate[proto] == 1.0
