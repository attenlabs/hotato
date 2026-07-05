#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run-demo.sh — regenerate the EXACT tool output embedded in the site demo
#   (hotato-site/docs/demo.html).
#
# This script only RUNS the tool; it does not modify tool source. It prints
# each command, then its verbatim output, for the three blocks on the page:
#
#   1. FAIL   : the bad-agent funnel-demo battery, text        (exit 1)
#   2. JSON   : the same battery, --format json (fix_map+funnel)(exit 1)
#   3. PASS   : the built-in self-test, 8/8, text              (exit 0)
#
# Usage:
#   examples/demo/run-demo.sh              # print the three blocks
#   examples/demo/run-demo.sh --out DIR    # also write raw captures to DIR
#                                          #   (fd-text.txt, fd-json.txt, pass-text.txt)
# ---------------------------------------------------------------------------
set -u

# repo root = two levels up from this script (examples/demo/ -> repo)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="src"
PY="${PYTHON:-python3}"

OUT_DIR=""
if [ "${1:-}" = "--out" ] && [ -n "${2:-}" ]; then
  OUT_DIR="$2"
  mkdir -p "$OUT_DIR"
fi

SCEN="examples/funnel-demo/scenarios"
AUDIO="examples/funnel-demo/audio"

hr(){ printf '%s\n' "----------------------------------------------------------------------"; }

run_block(){
  # $1 = label, $2 = out-file (or ""), rest = argv after the module
  local label="$1"; shift
  local outfile="$1"; shift
  hr
  printf '### %s\n' "$label"
  printf '$ PYTHONPATH=src %s -m hotato.cli %s\n\n' "$PY" "$*"
  # merge stderr into stdout: the self-test prints a "note:" line on stderr, and
  # a terminal view shows both. 2>&1 keeps the captured bytes identical to what
  # the page embeds.
  if [ -n "$outfile" ] && [ -n "$OUT_DIR" ]; then
    "$PY" -m hotato.cli "$@" 2>&1 | tee "$OUT_DIR/$outfile"
  else
    "$PY" -m hotato.cli "$@" 2>&1
  fi
  printf '\n(exit_code above is emitted by the tool itself)\n'
}

run_block "1) BAD-AGENT BATTERY (text) -> FAIL, exit 1" "fd-text.txt" \
  run --suite barge-in --scenarios "$SCEN" --audio "$AUDIO" --format text

run_block "2) BAD-AGENT BATTERY (json) -> fix_map + funnel pointer, exit 1" "fd-json.txt" \
  run --suite barge-in --scenarios "$SCEN" --audio "$AUDIO" --format json

run_block "3) BUILT-IN SELF-TEST (text) -> 8/8 PASS, exit 0" "pass-text.txt" \
  run --suite barge-in --format text

hr
printf 'Done. These three blocks are what hotato-site/docs/demo.html embeds verbatim.\n'
if [ -n "$OUT_DIR" ]; then
  printf 'Raw captures written to: %s\n' "$OUT_DIR"
fi
