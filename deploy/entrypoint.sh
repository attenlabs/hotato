#!/bin/sh
# Entrypoint for the hotato self-host workspace container.
#
# It resolves the bearer token from a Docker secret or the env file WITHOUT ever
# putting the secret on the process command line (where `docker inspect` / `ps`
# could read it), then execs the workspace server. If no token is supplied, the
# server generates one and stores it 0600 under the registry volume on first
# start -- read it with:
#
#   docker compose exec hotato-workspace cat /data/serve/default/token
#
# Precedence (matches deploy/healthcheck.py):
#   1. Docker secret  /run/secrets/hotato_token
#   2. env var        $HOTATO_SERVE_TOKEN  (written to a 0600 file, then passed
#                     as --token-file so it is never an argv the host can see)
#   3. none           -> the server generates + persists one
#
# The base command comes from the image CMD (hotato serve ...); this script only
# appends the token flag when a token was provided.
set -eu

SECRET_FILE="/run/secrets/hotato_token"
TOKEN_ARG=""

if [ -f "$SECRET_FILE" ]; then
    TOKEN_ARG="--token-file $SECRET_FILE"
elif [ -n "${HOTATO_SERVE_TOKEN:-}" ]; then
    umask 077
    printf '%s\n' "$HOTATO_SERVE_TOKEN" > /tmp/hotato-token
    TOKEN_ARG="--token-file /tmp/hotato-token"
fi

# shellcheck disable=SC2086  # word-splitting TOKEN_ARG into 0 or 2 args is intended
exec "$@" $TOKEN_ARG
