#!/usr/bin/env bash
# Run the build's test step against exactly the files `gcloud builds submit` would upload, before
# submitting. Catches failures that only appear in Cloud Build (a file kept out by .gcloudignore, a
# test that needs a local-only path) in about a minute instead of after a 25-minute build.
#
#   autopilot/build/upload-preflight.sh            # from the repository root
#   PYTHON=/path/to/python autopilot/build/upload-preflight.sh
#
# Steps: list the upload set with `gcloud meta list-files-for-upload`, copy it to a temporary
# directory, run the autopilot suite there in full and collect the main suite (collection is where
# environment-only failures show; the full main suite runs in the build itself). HOME points at an
# empty directory, as in the build container. Exit 0 = the build's test step would get this far.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
PY=${PYTHON:-python3}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/src" "$tmp/home"
( cd "$ROOT" && gcloud meta list-files-for-upload . ) > "$tmp/files"
[ -s "$tmp/files" ] || { echo "upload-preflight: gcloud listed no files" >&2; exit 2; }
( cd "$ROOT" && tr '\n' '\0' < "$tmp/files" | xargs -0 cp --parents -t "$tmp/src" )
echo "upload-preflight: $(wc -l < "$tmp/files") files in the upload set"
cd "$tmp/src"
HOME="$tmp/home" PYTHONPATH=src "$PY" -m pytest -q -p no:cacheprovider --collect-only >"$tmp/collect" 2>&1 \
  || { tail -25 "$tmp/collect"; echo "upload-preflight: main suite does not collect in the upload set" >&2; exit 1; }
tail -1 "$tmp/collect"
( cd autopilot && HOME="$tmp/home" "$PY" -m pytest -q -p no:cacheprovider tests 2>&1 | tail -3 ) | tee "$tmp/auto"
grep -qE ' failed|error' "$tmp/auto" && { echo "upload-preflight: autopilot tests fail in the upload set" >&2; exit 1; }
echo "upload-preflight: ok"
