#!/usr/bin/env bash
# Owner step: build the private Atlas collector (hotato-atlas) wheel from a local checkout and
# upload it to this project's private Python repository (infra/main.tf private_py), where the
# image build's atlas-wheel step picks it up (build/cloudbuild.yaml).
#
#   autopilot/build/publish-atlas-wheel.sh <hotato-atlas checkout>            # build and check only
#   GCLOUD_CONFIGURATION=<name> autopilot/build/publish-atlas-wheel.sh <hotato-atlas checkout> --upload
#
# - The wheel is built from the checkout's last commit (git archive HEAD), never from uncommitted
#   files, data or site output. Only the wheel is built: no source archive is made or uploaded.
# - The version is the collector's own pyproject version. It must equal _ATLAS_VERSION in
#   build/cloudbuild.yaml, and a version already in the repository is never replaced: bump the
#   collector's version, commit it there, then change the pin here.
# - The upload uses a short-lived access token of the named gcloud configuration, passed to twine
#   in its environment only: never printed, never on a command line, never written to disk.
#
# Environment: GCLOUD_CONFIGURATION (required for --upload), PROJECT (default hotato-autopilot),
# REGION (default us-central1), REPO (default hotato-private-py), KEEP_WHEEL (a directory to keep
# a copy of the built wheel in; it is otherwise deleted).
set -euo pipefail

die() { echo "publish-atlas-wheel: $*" >&2; exit 1; }

SRC=${1:-}
MODE=${2:-}
[ -n "$SRC" ] || die "usage: $0 <hotato-atlas checkout> [--upload]"
[ -z "$MODE" ] || [ "$MODE" = "--upload" ] || die "unknown option: $MODE"
SRC=$(cd "$SRC" && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)
PROJECT=${PROJECT:-hotato-autopilot}
REGION=${REGION:-us-central1}
REPO=${REPO:-hotato-private-py}
URL="https://$REGION-python.pkg.dev/$PROJECT/$REPO/"

git -C "$SRC" rev-parse --verify -q HEAD >/dev/null || die "$SRC is not a git checkout"
NAME=$(git -C "$SRC" show HEAD:pyproject.toml | python3 -c 'import sys,tomllib; print(tomllib.loads(sys.stdin.read())["project"]["name"])')
VERSION=$(git -C "$SRC" show HEAD:pyproject.toml | python3 -c 'import sys,tomllib; print(tomllib.loads(sys.stdin.read())["project"]["version"])')
[ "$NAME" = "hotato-atlas" ] || die "$SRC is not the hotato-atlas package (name: $NAME)"
PIN=$(sed -n 's/^  _ATLAS_VERSION: "\(.*\)"$/\1/p' "$HERE/cloudbuild.yaml")
[ "$VERSION" = "$PIN" ] || die "collector version $VERSION != _ATLAS_VERSION $PIN in build/cloudbuild.yaml"
if [ -n "$(git -C "$SRC" status --porcelain -- pyproject.toml src)" ]; then
  echo "note: uncommitted changes under src/ or pyproject.toml are NOT in this wheel (built from HEAD $(git -C "$SRC" rev-parse --short HEAD))"
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git -C "$SRC" archive --format=tar HEAD pyproject.toml src | tar -x -C "$WORK"
rm -rf "$WORK"/src/*.egg-info

python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q --disable-pip-version-check build==1.2.2 twine==6.1.0
"$WORK/venv/bin/python" -m build --wheel --outdir "$WORK/dist" "$WORK" >/dev/null

WHEEL="$WORK/dist/hotato_atlas-$VERSION-py3-none-any.whl"
[ -f "$WHEEL" ] || die "expected $(basename "$WHEEL"), got: $(ls "$WORK/dist")"
# The wheel may hold only the atlas package and its metadata.
"$WORK/venv/bin/python" - "$WHEEL" "$VERSION" <<'PY'
import sys, zipfile
wheel, version = sys.argv[1], sys.argv[2]
names = zipfile.ZipFile(wheel).namelist()
allowed = ("atlas/", f"hotato_atlas-{version}.dist-info/")
bad = [n for n in names if not n.startswith(allowed)]
if bad or not any(n == "atlas/cli.py" for n in names) or not any(n == "atlas/distill.py" for n in names):
    sys.exit(f"unexpected wheel contents: {bad[:10] or 'atlas/cli.py or atlas/distill.py missing'}")
print(f"wheel ok: {len(names)} files")
PY
"$WORK/venv/bin/twine" check "$WHEEL"
sha256sum "$WHEEL" | sed "s#$WORK/dist/##"
if [ -n "${KEEP_WHEEL:-}" ]; then mkdir -p "$KEEP_WHEEL" && cp "$WHEEL" "$KEEP_WHEEL/"; echo "copy kept in $KEEP_WHEEL"; fi

if [ "$MODE" != "--upload" ]; then
  echo "built and checked only; add --upload to publish $NAME $VERSION to $URL"
  exit 0
fi

[ -n "${GCLOUD_CONFIGURATION:-}" ] || die "set GCLOUD_CONFIGURATION to the gcloud configuration that uploads"
g() { gcloud --configuration "$GCLOUD_CONFIGURATION" --project "$PROJECT" "$@"; }
g artifacts repositories describe "$REPO" --location "$REGION" --format='value(format)' | grep -qx PYTHON \
  || die "repository $REPO (PYTHON) not found in $PROJECT/$REGION: apply autopilot/infra first"
if g artifacts versions list --package "$NAME" --repository "$REPO" --location "$REGION" \
     --format='value(name.basename())' 2>/dev/null | grep -qx "$VERSION"; then
  die "$NAME $VERSION is already in $REPO; versions are never replaced (bump the collector's version)"
fi

TWINE_USERNAME=oauth2accesstoken
TWINE_PASSWORD=$(gcloud --configuration "$GCLOUD_CONFIGURATION" auth print-access-token)
export TWINE_USERNAME TWINE_PASSWORD
"$WORK/venv/bin/twine" upload --non-interactive --disable-progress-bar --repository-url "$URL" "$WHEEL"
unset TWINE_PASSWORD
echo "uploaded $NAME $VERSION to $URL"
