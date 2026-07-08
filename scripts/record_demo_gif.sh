#!/usr/bin/env bash
#
# Deterministically re-records docs/assets/hotato-demo.gif from the real
# operator-recorded corpus clip (vapi-default-10-quiet-interrupt).
#
# Why a wrapper and not just `vhs .github/assets/hotato-demo.tape`:
#   1. hotato is not published, so the pasted `uvx hotato ...` commands need a
#      local shim that forwards to the checkout (no network, no PyPI).
#   2. the fixture-create step fails with `error: ... already exists` if its
#      output dir is stale; we clean it first so the recorded run succeeds and
#      the GIF's final frame is the SUCCESS state.
#   3. GUARD: we run the exact recorded battery once, headless, and FAIL if any
#      step prints a raw `error:` line (this is the regression the old GIF
#      shipped: it looped ending on a fixture-already-exists error).
#
# Requirements: vhs (go install github.com/charmbracelet/vhs@latest), ffmpeg,
# ttyd, a Chromium. Run from anywhere.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v vhs >/dev/null 2>&1 || {
  echo "vhs not found on PATH. Install: go install github.com/charmbracelet/vhs@latest" >&2
  exit 1
}

CLIP="corpus/vapi-defaults/audio/vapi-default-10-quiet-interrupt.example.wav"
[ -f "$CLIP" ] || { echo "missing corpus clip: $CLIP" >&2; exit 1; }

# 1) Local `uvx hotato` shim -> the checkout.
SHIM="$(mktemp -d)"
trap 'rm -rf "$SHIM" "$GUARD_OUT" "$LOG"' EXIT
cat > "$SHIM/uvx" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "hotato" ]; then shift; exec python3 -m hotato "$@"; fi
echo "gif shim only handles 'hotato', got: $*" >&2
exit 1
EOF
chmod +x "$SHIM/uvx"
export PATH="$SHIM:$PATH"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# 2) GUARD: run the recorded battery once into a throwaway dir; the last line
#    must be a success, and no step may print a raw `error:` line.
GUARD_OUT="$(mktemp -d)"
LOG="$(mktemp)"
{
  uvx hotato scan --stereo "$CLIP"
  uvx hotato run --stereo "$CLIP" --onset 2.00 --expect yield
  uvx hotato fixture create --stereo "$CLIP" --onset 2.00 --expect yield \
    --id demo-missed-interrupt --out "$GUARD_OUT/f"
} >"$LOG" 2>&1 || { echo "GUARD FAILED: battery exited nonzero:" >&2; cat "$LOG" >&2; exit 1; }

if grep -niE '^[[:space:]]*error:' "$LOG"; then
  echo "GUARD FAILED: the recorded battery prints an 'error:' line (the GIF must never end on one)." >&2
  exit 1
fi
LAST_LINE="$(grep -v '^[[:space:]]*$' "$LOG" | tail -n1)"
case "$LAST_LINE" in
  *[Ee]rror*) echo "GUARD FAILED: final output line is an error: $LAST_LINE" >&2; exit 1 ;;
esac
echo "guard passed. final battery line: $LAST_LINE"

# 3) Clean the VISIBLE fixture dir so the recorded create succeeds.
rm -rf /tmp/hotato-demo-fixture

# 4) Record.
vhs .github/assets/hotato-demo.tape
echo "wrote $ROOT/docs/assets/hotato-demo.gif"
