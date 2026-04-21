"""Config + topology-YAML rendering via Jinja2.

The output layout under ``<workdir>`` is:

    <workdir>/
        topology.clab.yml       # rendered clab topology
        configs/
            <node>/
                frr.conf        # vendor config (FRR only in phase 2)
                daemons         # FRR daemons toggle file

Jinja2 loader order:

1. The topology-specific ``template_dir`` (highest precedence). A topology
   that ships its own ``topology.clab.yml.j2`` or ``daemons.j2`` fully
   overrides the shared copy.
2. ``harness/_templates/shared/`` — shared ``topology.clab.yml.j2`` +
   ``daemons.j2``. Lets the 9 Phase-7 topologies keep only their
   vendor-specific ``frr.conf.j2`` in their own template dir.

The rendered filename is the template name minus one trailing ``.j2``. Any
``*.j2`` found in either loader path (minus the top-level clab YAML) is
rendered once per node.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from harness.topology import TopologySpec

_TOPO_YAML_TEMPLATE = "topology.clab.yml.j2"
_SHARED_TEMPLATE_DIR = Path(__file__).resolve().parent / "_templates" / "shared"


def render_topology(spec: TopologySpec, workdir: Path) -> Path:
    """Render the full topology (clab YAML + per-node configs) under ``workdir``.

    Returns the path to the clab YAML so the caller can ``containerlab deploy -t``
    it immediately. Creates ``workdir`` if missing. Overwrites any existing
    rendered files so reruns are idempotent.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    env = _build_env(spec)
    clab_path = _render_clab_yaml(spec, env, workdir)
    for node in spec.nodes:
        if node.adapter.kind == "bridge":
            continue  # bridge nodes have no per-node config templates
        _render_node_configs(spec, node, env, workdir)
    return clab_path


def _build_env(spec: TopologySpec) -> Environment:
    loaders = [FileSystemLoader(str(spec.template_dir))]
    if _SHARED_TEMPLATE_DIR.is_dir():
        loaders.append(FileSystemLoader(str(_SHARED_TEMPLATE_DIR)))
    return Environment(
        loader=ChoiceLoader(loaders),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _render_clab_yaml(spec: TopologySpec, env: Environment, workdir: Path) -> Path:
    tmpl = env.get_template(_TOPO_YAML_TEMPLATE)
    out = tmpl.render(spec=spec)
    path = workdir / "topology.clab.yml"
    path.write_text(out)
    return path


def _render_node_configs(spec: TopologySpec, node, env: Environment, workdir: Path) -> None:
    node_dir = workdir / "configs" / node.name
    node_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for tmpl_path in _node_template_paths(spec):
        if tmpl_path.name == _TOPO_YAML_TEMPLATE or tmpl_path.name in seen:
            continue
        seen.add(tmpl_path.name)
        tmpl = env.get_template(tmpl_path.name)
        out = tmpl.render(spec=spec, node=node, params=node.params)
        filename = tmpl_path.name[: -len(".j2")]
        (node_dir / filename).write_text(out)


def _node_template_paths(spec: TopologySpec):
    """Yield every ``*.j2`` in topology + shared dirs, topology first."""
    yield from spec.template_dir.glob("*.j2")
    if _SHARED_TEMPLATE_DIR.is_dir():
        yield from _SHARED_TEMPLATE_DIR.glob("*.j2")
