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

    If ``spec.external_renderer`` is set, the Jinja path is skipped entirely
    and the callable is invoked with ``workdir / "configs"`` so it can
    populate the layout directly. That mode is intended for synthetic
    large-scale fixtures (fat-tree k=64, enterprise_10000, ...) that are
    impractical to express as tuples of ``Node`` + ``Link``. sim-only
    runs are the only supported path for externally-rendered topologies;
    ``workdir / "topology.clab.yml"`` is still touched (empty) so callers
    that blindly return its path don't crash on ``FileNotFoundError``.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    if spec.external_renderer is not None:
        configs_dir = workdir / "configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        spec.external_renderer(configs_dir)
        clab_path = workdir / "topology.clab.yml"
        clab_path.write_text("# external_renderer topology — sim-only only\n")
        return clab_path
    env = _build_env(spec)
    clab_path = _render_clab_yaml(spec, env, workdir)
    for node in spec.nodes:
        if not node.adapter.config_template_names:
            continue  # bridges (and any future zero-config adapter) skip config rendering
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
    """Render only the templates this node's adapter declares.

    ``node.adapter.config_template_names`` is the allow-list: anything else
    under ``spec.template_dir`` (or the shared dir) is either a different
    vendor's template (mixed-vendor topology — e.g. FRR frr.conf.j2 on a
    cEOS node) or the top-level clab YAML itself. Filtering here means
    each node only gets its own vendor's config files.
    """
    node_dir = workdir / "configs" / node.name
    node_dir.mkdir(parents=True, exist_ok=True)
    for tmpl_name in node.adapter.config_template_names:
        tmpl = env.get_template(tmpl_name)
        out = tmpl.render(spec=spec, node=node, params=node.params)
        filename = tmpl_name[: -len(".j2")] if tmpl_name.endswith(".j2") else tmpl_name
        (node_dir / filename).write_text(out)
