#!/usr/bin/env bash
# Fake $HAMMERHEAD_CLI for phase-1 through phase-5 development.
#
# Real invocation contract:
#     $HAMMERHEAD_CLI simulate <configs_dir> --format json
#
# Fake behaviour (phase 6): read a vendor-truth NodeFib JSON (pointed to by
# FAKE_HAMMERHEAD_SOURCE), lightly perturb it (drop one route + inject one
# bogus next-hop), emit the result on stdout. That way the diff engine has a
# non-empty happy AND sad path to test against while the real Hammerhead
# wrapper is being built.
#
# Until phase 6 this script just emits an empty SimulateView stub so the
# preflight pipeline can reference it without exploding.
set -euo pipefail

if [[ "${1:-}" != "simulate" ]]; then
    echo "fake_hammerhead.sh: expected 'simulate' subcommand, got '${1:-<none>}'" >&2
    exit 2
fi

# Phase 1 stub output. Real perturbation lands with phase 6.
cat <<'JSON'
{
  "devices": [],
  "_stub": true,
  "_note": "fake_hammerhead.sh phase-1 stub; see scripts/fake_hammerhead.sh for phase-6 contract"
}
JSON
