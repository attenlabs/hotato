#!/bin/sh
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
PYTHON=${PYTHON:-python3}

# A source checkout does not need an editable install. An installed package is
# used when this example is copied elsewhere.
if [ -d "$REPO/src/hotato" ]; then
  if [ -n "${PYTHONPATH:-}" ]; then
    PYTHONPATH="$REPO/src:$PYTHONPATH"
  else
    PYTHONPATH="$REPO/src"
  fi
  export PYTHONPATH
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/hotato-counterexample-example.XXXXXX")
PRIVATE="$WORK/refund-not-posted.hotato-repro"
SHARE="$WORK/refund-not-posted.share-safe"

"$PYTHON" -m hotato counterexample compile \
  --scenario "$HERE/refund-not-posted.scenario.json" \
  --test "$HERE/refund-not-posted.test.json" \
  --target refund-posted \
  --workspace "$HERE" \
  --out "$PRIVATE"

"$PYTHON" -m hotato counterexample verify "$PRIVATE"
"$PYTHON" -m hotato counterexample reproduce "$PRIVATE"
"$PYTHON" -m hotato counterexample inspect "$PRIVATE" --format json

# Predicate semantics are deliberately the inverse of reproduce: a preserved
# failure is a bad revision for git-bisect purposes, so the expected exit is 1.
set +e
"$PYTHON" -m hotato counterexample predicate "$PRIVATE"
predicate_rc=$?
set -e
if [ "$predicate_rc" -ne 1 ]; then
  echo "unexpected predicate exit: $predicate_rc (expected 1)" >&2
  exit 1
fi
printf 'predicate exit: %s (target failure present)\n' "$predicate_rc"

"$PYTHON" -m hotato counterexample export "$PRIVATE" \
  --profile share-safe-v1 \
  --out "$SHARE"

printf '\nprivate runnable capsule: %s\n' "$PRIVATE"
printf 'share-safe projection:   %s\n' "$SHARE"
