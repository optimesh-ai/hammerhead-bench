#!/usr/bin/env bash
# Per-topology bench driver. Docker Desktop on macOS leaks port 9997 on fast
# teardown→re-bind, so we run each topology in a separate hammerhead-bench
# invocation and sleep 3s between them for the port to actually release.
#
# This is slower than a single `--trials 5` invocation but reliable.
set -euo pipefail

: "${HAMMERHEAD_CLI:?must be set}"
TRIALS="${TRIALS:-5}"
SKIP="${SKIP:-fat-tree-k64 acl-semantics-3node}"

cd "$(dirname "$0")/.."

topologies=(
  acl-heavy-parse
  bgp-ebgp-2node
  bgp-ibgp-2node
  hub-spoke-wan-51node
  isis-l1l2-4node
  mixed-vendor-frr-ceos-4node
  mpls-l3vpn-4node
  multi-as-edge-5node
  ospf-broadcast-4node
  ospf-p2p-3node
  route-map-pathological
  route-reflector-6node
  spine-leaf-6node
  spine-leaf-20node
  spine-leaf-50node
  spine-leaf-100node
)

passed=0
failed=()

docker ps -a --filter "ancestor=batfish/allinone:latest" -q | xargs -r docker rm -f >/dev/null 2>&1 || true
sleep 2

for topo in "${topologies[@]}"; do
  echo "=== $topo ==="
  if uv run hammerhead-bench bench --trials "$TRIALS" --sim-only --only "$topo"; then
    passed=$((passed + 1))
  else
    failed+=("$topo")
  fi
  # Clean up any lingering batfish container + let kernel release :9997.
  docker ps -a --filter "ancestor=batfish/allinone:latest" -q | xargs -r docker rm -f >/dev/null 2>&1 || true
  sleep 3
done

echo
echo "Passed: $passed/${#topologies[@]}"
if (( ${#failed[@]} > 0 )); then
  echo "Failed: ${failed[*]}"
  exit 1
fi
