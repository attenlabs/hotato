#!/usr/bin/env bash
# Resolves the bundled bad/good audio paths from the installed hotato package
# and runs the compare step of the README demo GIF. Used only by
# hotato-demo.tape (vhs) to render docs/assets/hotato-demo.gif; not part of
# the CLI and not shipped in the sdist/wheel.
set -euo pipefail

BAD=$(uvx --from hotato python3 -c "from importlib import resources; print(resources.files('hotato').joinpath('data', 'demo', 'failing', 'audio', 'fd-01-missed-interruption.example.wav'))")
GOOD=$(uvx --from hotato python3 -c "from importlib import resources; print(resources.files('hotato').joinpath('data', 'audio', '01-hard-interruption.example.wav'))")

uvx hotato compare --before "$BAD" --after "$GOOD" \
    --before-onset 2.00 --after-onset 2.40 --expect yield
