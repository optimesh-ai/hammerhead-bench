"""Template rendering tests for the bgp-ibgp-2node topology.

Phase 2 ships one topology end-to-end. These tests lock the rendered clab YAML
and frr.conf shape so a Jinja2 refactor in phase 7 (when we add 9 more
topologies) can't silently break bgp-ibgp-2node.
"""

from __future__ import annotations

from pathlib import Path

from harness.render import render_topology
from harness.topology import load_spec

TOPO_DIR = Path(__file__).resolve().parent.parent / "topologies" / "bgp-ibgp-2node"


def test_spec_loads_from_topo_py() -> None:
    spec = load_spec(TOPO_DIR)
    assert spec.name == "bgp-ibgp-2node"
    assert [n.name for n in spec.nodes] == ["r1", "r2"]
    assert spec.links[0].a == ("r1", "eth1")
    assert spec.links[0].b == ("r2", "eth1")


def test_render_writes_clab_yaml_and_per_node_configs(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    clab_yaml = render_topology(spec, tmp_path)

    assert clab_yaml.exists()
    assert clab_yaml.name == "topology.clab.yml"
    for node in ("r1", "r2"):
        assert (tmp_path / "configs" / node / "frr.conf").exists()
        assert (tmp_path / "configs" / node / "daemons").exists()


def test_rendered_clab_yaml_has_memory_caps_and_link(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    clab_yaml = render_topology(spec, tmp_path)
    content = clab_yaml.read_text()

    assert "name: hh-bench-bgp-ibgp-2node" in content
    assert "memory: 256m" in content
    assert "frrouting/frr:v8.4.1" in content
    # Exactly one link, bidirectional endpoint form.
    assert 'endpoints: ["r1:eth1", "r2:eth1"]' in content


def test_rendered_frr_conf_has_ibgp_neighbor_on_loopback(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    render_topology(spec, tmp_path)
    r1_conf = (tmp_path / "configs" / "r1" / "frr.conf").read_text()

    assert "hostname r1" in r1_conf
    assert "ip address 10.0.0.1/32" in r1_conf
    assert "ip address 10.0.12.1/30" in r1_conf
    assert "router bgp 65100" in r1_conf
    assert "neighbor 10.0.0.2 remote-as 65100" in r1_conf
    assert "neighbor 10.0.0.2 update-source lo" in r1_conf
    assert "network 10.0.0.1/32" in r1_conf
    # Static /32 for peer loopback so iBGP can come up before IGP learn.
    assert "ip route 10.0.0.2/32 10.0.12.2" in r1_conf


def test_rendered_daemons_enables_bgp_and_staticd_only(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    render_topology(spec, tmp_path)
    daemons = (tmp_path / "configs" / "r1" / "daemons").read_text()

    assert "zebra=yes" in daemons
    assert "bgpd=yes" in daemons
    assert "staticd=yes" in daemons
    assert "ospfd=no" in daemons
    assert "isisd=no" in daemons
    assert "vtysh_enable=yes" in daemons


def test_render_is_idempotent(tmp_path: Path) -> None:
    spec = load_spec(TOPO_DIR)
    render_topology(spec, tmp_path)
    before = (tmp_path / "configs" / "r1" / "frr.conf").read_text()
    render_topology(spec, tmp_path)
    after = (tmp_path / "configs" / "r1" / "frr.conf").read_text()
    assert before == after
