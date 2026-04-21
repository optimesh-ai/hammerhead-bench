"""hammerhead-bench: accuracy + speed benchmark for network simulators.

Public surface is small: the CLI entry point at `harness.cli:main`.
Internals are organized into adapters (vendor extraction), extract (canonical
FIB types), tools (Batfish + Hammerhead wrappers), diff (comparison engine),
and report (HTML + Markdown generators). Memory and pipeline orchestration
live at the top level.
"""

__version__ = "0.1.0"
