#!/usr/bin/env bash
# Fake $HAMMERHEAD_CLI for offline smoke + end-to-end development.
#
# Real invocation contract the harness shells out to (see
# harness/tools/hammerhead.py::SubprocessHammerheadRunner, bulk-emit
# path as of 2026-04-22):
#
#     $HAMMERHEAD_CLI simulate <configs_dir> --emit-rib all --format json
#
# Fake behaviour: read the vendor-truth NodeFib JSON files from
# FAKE_HAMMERHEAD_SOURCE_DIR (one `<node>__default.json` per device,
# as written by the FRR adapter), perturb them lightly, and emit output
# that matches Hammerhead's bulk-emit JSON shape exactly, i.e.
#
#     {"rib": {"<hostname>": {"hostname": "...", "entries": [...]}}}
#
# Perturbation, per docs/SPEC:
#   - drop the first BGP route (exercises "vendor-only" presence)
#   - inject a bogus next-hop on the first OSPF route (exercises next-
#     hop mismatch)
# These deliberately create both coverage-gap and correctness-gap diff
# rows so the diff engine + report have both a happy and a sad path.
#
# Legacy compatibility: the pre-migration harness also shelled out to
# `simulate <dir> --format json` (device discovery) and
# `rib --device <X> --format json` (per-device RIB). Those subcommand
# shapes are preserved here so any stale caller (older bench branch,
# a rebase, or a test that hasn't been ported yet) still gets sane
# output.
#
# Environment:
#   FAKE_HAMMERHEAD_SOURCE_DIR  dir containing <node>__default.json files
#                               (typically results/vendor_truth/<topology>)
#   FAKE_HAMMERHEAD_PERTURB     "1" (default) to perturb; "0" to emit
#                               byte-identical-modulo-schema output
#
# Requires: jq (pre-existing preflight requirement).
set -euo pipefail

die() { echo "fake_hammerhead.sh: $*" >&2; exit 2; }

subcmd="${1:-}"
emit_rib=""
bulk_mode=0
case "$subcmd" in
    simulate)
        shift
        configs_dir=""
        # First non-flag arg is the configs dir.
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --emit-rib) emit_rib="${2:-}"; shift 2 ;;
                --format)                       shift 2 ;;
                --*)                            shift   ;;
                *)
                    if [[ -z "$configs_dir" ]]; then
                        configs_dir="$1"
                    fi
                    shift
                    ;;
            esac
        done
        if [[ -n "$emit_rib" ]]; then
            [[ "$emit_rib" == "all" ]] || die \
                "unsupported --emit-rib value '$emit_rib' (fake only serves 'all')"
            bulk_mode=1
        fi
        ;;
    rib)
        # Legacy per-device path. Parse --config-dir <dir> --device <name>
        # --format json; also accept a positional configs dir (post-2026-04
        # CLI shape).
        shift
        configs_dir=""
        device=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --config-dir) configs_dir="${2:-}"; shift 2 ;;
                --device)     device="${2:-}";       shift 2 ;;
                --format)                             shift 2 ;;
                --*)                                  shift   ;;
                *)
                    if [[ -z "$configs_dir" ]]; then
                        configs_dir="$1"
                    fi
                    shift
                    ;;
            esac
        done
        [[ -n "$device" ]] || die "rib: missing --device"
        ;;
    *)
        die "expected 'simulate' or 'rib', got '${subcmd:-<none>}'"
        ;;
esac

source_dir="${FAKE_HAMMERHEAD_SOURCE_DIR:-}"
[[ -n "$source_dir" && -d "$source_dir" ]] || die \
    "FAKE_HAMMERHEAD_SOURCE_DIR not set or not a directory: '${source_dir}'"

perturb="${FAKE_HAMMERHEAD_PERTURB:-1}"

# jq program that translates one vendor-truth NodeFib JSON into the
# ``entries[]`` shape Hammerhead's rib/emit-rib output uses. Shared
# between the bulk and legacy per-device paths so perturbation stays
# identical regardless of which subcommand was invoked.
_entries_for_file() {
    local src="$1"
    local entries
    entries=$(jq '
        def proto_code: if . == "connected" then "C"
                       elif . == "static"    then "S"
                       elif . == "bgp"       then "B"
                       elif . == "ospf"      then "O"
                       elif . == "isis"      then "i L1"
                       elif . == "rip"       then "R"
                       else "O" end;

        [ .routes[] |
          {
            prefix: .prefix,
            protocol: (.protocol | proto_code),
            admin_distance: (.admin_distance // 0),
            metric: (.metric // 0),
            next_hop_interface: (.next_hops[0].interface // null),
            next_hop_ip: (.next_hops[0].ip // null),
            tag: 0,
            bgp: (if .protocol == "bgp" then
                    {as_path: (.as_path // []),
                     local_preference: (.local_pref // 100),
                     med: (.med // 0),
                     origin: "igp",
                     communities: (.communities // []),
                     weight: 0}
                  else null end),
            ospf: (if .protocol == "ospf" then
                     {route_type: "intra-area"}
                   else null end)
          }
        ]
    ' "$src")

    if [[ "$perturb" == "1" ]]; then
        # 1) Drop the first BGP route (if any) → vendor-only presence.
        # 2) Inject a bogus next-hop on the first OSPF route (if any)
        #    → next-hop mismatch.
        entries=$(echo "$entries" | jq '
            . as $in |
            (map(.protocol == "B") | index(true)) as $bgp_i |
            (map(.protocol == "O") | index(true)) as $ospf_i |
            (if $bgp_i == null then $in else $in | del(.[$bgp_i]) end)
            | if $ospf_i == null then .
              else .[$ospf_i].next_hop_ip = "169.254.99.99"
                 | .[$ospf_i].next_hop_interface = "bogus0"
              end
        ')
    fi
    echo "$entries"
}

case "$subcmd" in
    simulate)
        if [[ "$bulk_mode" == "1" ]]; then
            # Bulk-emit path: one object, keyed by hostname, each value
            # is a {hostname, entries} pair. Assembled by letting jq
            # merge per-device objects via `-s add`, so we never splice
            # raw JSON fragments by hand.
            mapfile -t fibs < <(find "$source_dir" -maxdepth 1 -name '*__default.json' | sort)
            tmp=$(mktemp)
            trap 'rm -f "$tmp"' EXIT
            : > "$tmp"
            for f in "${fibs[@]}"; do
                node=$(jq -r '.node' "$f")
                entries=$(_entries_for_file "$f")
                jq -n \
                    --arg k "$node" \
                    --arg h "$node" \
                    --argjson e "$entries" \
                    '{($k): {hostname:$h, entries:$e}}' \
                    >> "$tmp"
            done
            # `-s add` merges each one-key object into a single map,
            # preserving the hostname → view pairs.
            jq -s 'add // {}' "$tmp" | jq '{rib: .}'
        else
            # Legacy "simulate --format json" (device discovery) path.
            mapfile -t fibs < <(find "$source_dir" -maxdepth 1 -name '*__default.json' | sort)
            devices="["
            first=1
            for f in "${fibs[@]}"; do
                node=$(jq -r '.node' "$f")
                route_count=$(jq -r '.routes | length' "$f")
                [[ $first -eq 1 ]] || devices+=","
                first=0
                devices+=$(jq -n \
                    --arg h "$node" \
                    --argjson r "$route_count" \
                    '{hostname:$h, rib_entry_count:$r, fib_entry_count:$r,
                      connected_routes:0, static_routes:0, ospf_routes:0,
                      bgp_routes:0, ospf_adjacencies:0, ospf_areas:[]}')
            done
            devices+="]"
            jq -n \
                --argjson devices "$devices" \
                --argjson n "${#fibs[@]}" \
                '{device_count:$n, link_count:0,
                  ospf_warning_count:0, topology_warning_count:0,
                  ospf_total_lsas:0,
                  parse_coverage:{lines_total:0, lines_classified:0,
                                  lines_unparsed:0, coverage_pct:100.0,
                                  per_device:[]},
                  devices:$devices}'
        fi
        ;;
    rib)
        src="$source_dir/${device}__default.json"
        [[ -f "$src" ]] || die "no vendor truth for device '$device' in $source_dir"
        entries=$(_entries_for_file "$src")
        jq -n \
            --arg h "$device" \
            --argjson e "$entries" \
            '{hostname:$h, entries:$e}'
        ;;
esac
