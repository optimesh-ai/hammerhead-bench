"""Topology spec — the in-memory description of a benchmark topology.

Each ``topologies/<name>/topo.py`` exposes a module-level ``SPEC: TopologySpec``
object. The pipeline imports it, renders the clab YAML + per-node configs, and
hands them to the matching vendor adapter.

Design notes:

- ``TopologySpec`` is the ONLY place we hard-code a topology's shape. Adapters
  stay vendor-generic; the pipeline stays topology-generic.
- Every ``Node`` carries an adapter instance (not a class) so per-topology
  customization (e.g. a node that needs 512 MB instead of the default 256) is a
  simple keyword override on the adapter constructor.
- ``links`` is a flat list of ``(node, iface) -> (node, iface)`` pairs so the
  clab YAML renders one link block per entry; no implicit link ordering.
- ``params`` is a free-form dict passed verbatim to the Jinja2 config template.
  We deliberately do NOT type it — every vendor has different knobs and we'd
  just end up with a union type hole.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.adapters.base import VendorAdapter


@dataclass(frozen=True, slots=True)
class Interface:
    """One physical interface on a node (clab link endpoint)."""

    name: str
    """clab interface name, e.g. ``eth1``. ``eth0`` is reserved for clab mgmt."""

    ip: str | None = None
    """Interface IP in CIDR form, e.g. ``10.0.12.1/30``. ``None`` for L2-only."""

    description: str | None = None


@dataclass(frozen=True, slots=True)
class Node:
    """One device in the topology."""

    name: str
    """Short name used in clab YAML + container name. Must be ASCII lowercase."""

    adapter: VendorAdapter
    """Vendor adapter instance. Picks memory_mb, image, daemons template, etc."""

    interfaces: tuple[Interface, ...] = field(default_factory=tuple)

    params: dict[str, Any] = field(default_factory=dict)
    """Free-form dict handed to the Jinja2 config template. Vendor-specific."""


@dataclass(frozen=True, slots=True)
class Link:
    """One point-to-point clab link. Endpoints in list form so clab renders as-is."""

    a: tuple[str, str]
    """``(node_name, iface_name)`` of endpoint A."""

    b: tuple[str, str]
    """``(node_name, iface_name)`` of endpoint B."""


@dataclass(frozen=True, slots=True)
class TopologySpec:
    """Full spec for one benchmark topology. One instance per ``topologies/<name>``."""

    name: str
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]
    template_dir: Path
    """Directory holding ``topology.clab.yml.j2`` + vendor config templates."""

    description: str = ""

    external_renderer: Callable[[Path], None] | None = None
    """Optional escape hatch for topologies too large to model as tuples of
    ``Node`` + ``Link``. When set, the harness renders the topology by
    invoking ``external_renderer(configs_dir)`` — the callable is expected
    to populate ``configs_dir`` with ``<host>.cfg`` (EOS / flat) or
    ``<host>/frr.conf`` (FRR / subdir) files in the same layout the
    Batfish + Hammerhead hooks already consume. The Jinja path is skipped
    entirely. sim-only mode is the only supported path in that case: no
    clab YAML is rendered so ``run_topology`` (with-truth) will still
    fail loudly on a missing ``topology.clab.yml.j2`` unless the caller
    implements it themselves.
    """

    def node(self, name: str) -> Node:
        """Return the node with the given name; raise KeyError if missing."""
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"no node named {name!r} in topology {self.name!r}")


def load_spec(topology_dir: Path) -> TopologySpec:
    """Import ``topology_dir / 'topo.py'`` and return its module-level ``SPEC``.

    Uses importlib with a private module name so two topologies named the same
    thing can't shadow each other. Raises a clear error if ``SPEC`` is missing
    or not a ``TopologySpec``.
    """
    topo_py = topology_dir / "topo.py"
    if not topo_py.exists():
        raise FileNotFoundError(f"{topo_py} does not exist")
    mod_name = f"_topo_{topology_dir.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, topo_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {topo_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    topo_spec = getattr(module, "SPEC", None)
    if not isinstance(topo_spec, TopologySpec):
        raise TypeError(
            f"{topo_py}:SPEC must be a TopologySpec, got {type(topo_spec).__name__}"
        )
    return topo_spec


# ----- FRR-only-truth eligibility -----------------------------------------

# Vendor-adapter ``kind`` values that can be brought up under containerlab on a
# Linux host without a proprietary image (FRR / Cumulus are both FRR-stack).
# Kept as a module-level frozenset so tests can assert on it directly.
_FRR_ONLY_TRUTH_ADAPTER_KINDS: frozenset[str] = frozenset({"frr", "cumulus_vx"})

# Containerlab resource ceiling on a typical laptop: past ~20 FRR containers
# memory + veth bring-up gets flaky, so we carve that cap in here rather than
# letting a large topology silently OOM a Linux CI runner.
FRR_ONLY_TRUTH_MAX_NODES: int = 20


def frr_only_truth_eligible(spec: TopologySpec) -> bool:
    """Return True when *spec* can run under the ``--frr-only-truth`` pipeline.

    The eligibility rules are:

    1. The spec must not rely on an ``external_renderer`` — those topologies
       bypass the Jinja + clab YAML path, so the 3-way pipeline can't rendered
       a clab YAML for them. (The ``--sim-only`` path is still fine.)
    2. Every node in the spec must use an adapter whose ``kind`` is one of
       :data:`_FRR_ONLY_TRUTH_ADAPTER_KINDS` — i.e. purely FRR / Cumulus.
       A single Cisco / Juniper node disqualifies the whole topology.
    3. The spec must have at most :data:`FRR_ONLY_TRUTH_MAX_NODES` nodes
       (inclusive). Larger topologies exceed the memory + veth budget of a
       typical containerlab-capable laptop / CI runner.

    This function is pure; callers run it before any container / subprocess
    work. It only inspects the ``TopologySpec`` in-memory so it's trivially
    unit-testable and safe to call from the CLI selection path.
    """
    if spec.external_renderer is not None:
        return False
    if len(spec.nodes) > FRR_ONLY_TRUTH_MAX_NODES:
        return False
    for node in spec.nodes:
        kind = getattr(node.adapter, "kind", None)
        if kind not in _FRR_ONLY_TRUTH_ADAPTER_KINDS:
            return False
    return True
