#!/usr/bin/env bash
# Nightly sweep: pull the last day of calls, write a dated dashboard plus the
# machine-readable candidate list `hotato fixture promote` reads. Meant to run
# from cron at the repo root; see README.md in this directory for the
# crontab entry and the CI half of the loop.
#
# No stack connected yet? Run it once with DEMO=1 to see the whole script
# work credential-less against the two bundled real demo calls:
#   DEMO=1 ./examples/set-and-forget/sweep-nightly.sh
set -euo pipefail

STACK="${HOTATO_STACK:-vapi}"
SINCE="${HOTATO_SINCE:-1d}"
OUT_DIR="${HOTATO_REPORT_DIR:-reports}"
DATE="$(date +%F)"

mkdir -p "$OUT_DIR"

if [ "${DEMO:-0}" = "1" ]; then
  SWEEP_ARGS=(--demo)
  echo "sweep-$DATE: demo mode, no credentials, no network"
else
  SWEEP_ARGS=(--stack "$STACK" --since "$SINCE")
  echo "sweep-$DATE: stack=$STACK since=$SINCE"
fi

hotato sweep "${SWEEP_ARGS[@]}" --format json > "$OUT_DIR/sweep-$DATE.json"
hotato sweep "${SWEEP_ARGS[@]}" --out "$OUT_DIR/sweep-$DATE.html" --no-open

python3 -c "
import json
with open('$OUT_DIR/sweep-$DATE.json') as f:
    data = json.load(f)
print(f\"sweep-$DATE: {data['total_candidates']} candidate moments across \"
      f\"{data['calls_scanned']} calls scanned, {data['calls_skipped']} skipped\")
"

echo "dashboard: $OUT_DIR/sweep-$DATE.html"
echo "promote a confirmed one with:"
echo "  hotato fixture promote $OUT_DIR/sweep-$DATE.json#N --expect yield|hold --id <slug> --out tests/hotato"
