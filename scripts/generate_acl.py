#!/usr/bin/env python3
"""Generate the 500-line overlapping ACL for the acl-heavy-parse topology.

Phase 1: stub. The real generator lands with phase 7 when we fill in the
acl-heavy-parse config templates. Keeping the script in place so the
topology's README can reference it and the layout is predictable.
"""

from __future__ import annotations

import sys


def main() -> int:  # pragma: no cover - phase 7
    print("generate_acl.py: phase 7 deliverable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
