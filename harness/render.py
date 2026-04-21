"""Config + topology-YAML rendering via Jinja2.

The output layout under ``<workdir>`` is:

    <workdir>/
        topology.clab.yml       # rendered clab topology
        configs/
            <node>/
                frr.conf        # vendor config (FRR only in phase 2)
                daemons         # FRR daemons toggle file

Any adapter that needs per-node rendering can add another template name; the
``render_node_configs`` function glob-renders every ``*.j2`` in the topology's
template_dir minus the top-level ``topology.clab.yml.j2``.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from harness.topology import TopologySpec

_TOPO_YAML_TEMPLATE = "topology.clab.yml.j2"


def render_topology(spec: TopologySpec, workdir: Path) -> Path:
    """Render the full topology (clab YAML + per-node configs) under ``workdir``.

    Returns the path to the clab YAML so the caller can ``containerlab deploy -t``
    it immediately. Creates ``workdir`` if missing. Overwrites any existing
    rendered files so reruns are idempotent.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(spec.template_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    clab_path = _render_clab_yaml(spec, env, workdir)
    for node in spec.nodes:
        _render_node_configs(spec, node, env, workdir)
    return clab_path


def _render_clab_yaml(spec: TopologySpec, env: Environment, workdir: Path) -> Path:
    tmpl = env.get_template(_TOPO_YAML_TEMPLATE)
    out = tmpl.render(spec=spec)
    path = workdir / "topology.clab.yml"
    path.write_text(out)
    return path


def _render_node_configs(spec: TopologySpec, node, env: Environment, workdir: Path) -> None:
    node_dir = workdir / "configs" / node.name
    node_dir.mkdir(parents=True, exist_ok=True)
    for tmpl_path in spec.template_dir.glob("*.j2"):
        if tmpl_path.name == _TOPO_YAML_TEMPLATE:
            continue
        tmpl = env.get_template(tmpl_path.name)
        out = tmpl.render(spec=spec, node=node, params=node.params)
        # Strip one trailing .j2 to get the real filename.
        filename = tmpl_path.name[: -len(".j2")]
        (node_dir / filename).write_text(out)
