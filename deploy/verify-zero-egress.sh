#!/usr/bin/env bash
#
# verify-zero-egress.sh — prove, on THIS machine, what the default hotato
# self-host stack does and does not do on the network.
#
# It checks three concrete, honest things:
#
#   1. PUBLISHED SURFACE. The workspace publishes exactly one port, only to host
#      loopback (127.0.0.1:8321). The Ollama judge, if present, publishes none.
#
#   2. ZERO-EGRESS PROOF (the core check). A throwaway container from the SAME
#      image is run on an *internal* Docker network where egress is physically
#      removed. We confirm (a) an outbound connection to a public host FAILS
#      there, so egress really is blocked, and then (b) the workspace still
#      answers a view with HTTP 200 over loopback. A server that serves with the
#      network unplugged is a server that needs no egress to do its job.
#
#   3. LIVE CONNECTIONS. On the running workspace container we list ESTABLISHED
#      TCP peers and confirm none are external (only loopback / the host-inbound
#      published port). Informational; reads /proc/net/tcp (ss if available).
#
# What this does NOT claim: that Docker firewalls the default stack. The default
# workspace runs on a normal bridge so its port can publish; the guarantee is the
# server's RUNTIME BEHAVIOUR (it opens no outbound connection), which check 2
# proves by removing egress and showing the server unaffected. Opt-in features
# that DO reach the network are listed in docs/EGRESS.md.
#
# Usage:  ./deploy/verify-zero-egress.sh
# Requires: docker, docker compose. Run from the repo root (where the compose
# file lives). Exit 0 = all checks passed; non-zero = a check failed.

set -euo pipefail

IMAGE="hotato-selfhost:local"
WORKSPACE_SVC="hotato-workspace"
CANARY_NET="hotato-zero-egress-canary-net"
CANARY_CTR="hotato-zero-egress-canary"
PASS=0
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [PASS] %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL + 1)); }
note() { printf '  [info] %s\n' "$*"; }

cleanup() {
  docker rm -f "$CANARY_CTR" >/dev/null 2>&1 || true
  docker network rm "$CANARY_NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
say "== 1. Published surface (only 127.0.0.1:8321, nothing else) =="

# The workspace must publish exactly one host binding: 127.0.0.1:8321.
mapping="$(docker compose port "$WORKSPACE_SVC" 8321 2>/dev/null || true)"
if [ -z "$mapping" ]; then
  note "workspace not running; starting it (docker compose up -d $WORKSPACE_SVC)"
  docker compose up -d "$WORKSPACE_SVC" >/dev/null
  sleep 3
  mapping="$(docker compose port "$WORKSPACE_SVC" 8321 2>/dev/null || true)"
fi

if printf '%s' "$mapping" | grep -q '^127\.0\.0\.1:8321$'; then
  ok "workspace published at $mapping (host loopback only)"
else
  bad "workspace port mapping is '$mapping' (expected 127.0.0.1:8321)"
fi

# No published binding may be on a wide interface.
wide="$(docker compose ps --format '{{.Publishers}}' 2>/dev/null \
        | grep -oE '0\.0\.0\.0:[0-9]+|\[::\]:[0-9]+' || true)"
if [ -z "$wide" ]; then
  ok "no service publishes on a wildcard interface (0.0.0.0 / [::])"
else
  bad "a service publishes on a wildcard interface: $wide"
fi

# The Ollama judge (if the profile is up) must publish nothing.
if docker compose ps --services 2>/dev/null | grep -qx "ollama"; then
  oll="$(docker compose port ollama 11434 2>/dev/null || true)"
  if [ -z "$oll" ]; then
    ok "ollama judge publishes no port (private compose network only)"
  else
    bad "ollama judge is published at $oll (should be unpublished)"
  fi
else
  note "ollama judge profile not running (nothing to check)"
fi

# ---------------------------------------------------------------------------
say ""
say "== 2. Zero-egress proof: same image, egress physically removed =="

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  note "image $IMAGE not built yet; building it (docker compose build $WORKSPACE_SVC)"
  docker compose build "$WORKSPACE_SVC" >/dev/null
fi

cleanup
docker network create --internal "$CANARY_NET" >/dev/null
# Run the REAL image + entrypoint (serve on 0.0.0.0:8321) with no egress route.
docker run -d --name "$CANARY_CTR" --network "$CANARY_NET" "$IMAGE" >/dev/null

# Wait for the server to come up (health/loopback), up to ~25s.
up=0
for _ in $(seq 1 25); do
  if docker exec "$CANARY_CTR" python3 /opt/hotato-deploy/healthcheck.py >/dev/null 2>&1; then
    up=1; break
  fi
  sleep 1
done

# (a) Egress must be physically blocked in this env. Use `python3 -c` (no stdin)
# so the probe is the container's own code, not a heredoc that `docker exec`
# would silently drop without `-i`.
if docker exec "$CANARY_CTR" python3 -c \
     "import socket; socket.setdefaulttimeout(4); socket.create_connection(('1.1.1.1', 53))" \
     >/dev/null 2>&1
then
  bad "an outbound connection SUCCEEDED in the canary — egress was not blocked, so (b) is not meaningful"
else
  ok "outbound connection to a public host is blocked (egress physically removed)"
fi

# (b) With egress removed, the workspace still serves a view (HTTP 200).
if [ "$up" = "1" ]; then
  ok "workspace answered a view with HTTP 200 while egress was removed (needs no egress)"
else
  bad "workspace did not answer over loopback within the timeout (see: docker logs $CANARY_CTR)"
fi
cleanup

# ---------------------------------------------------------------------------
say ""
say "== 3. Live connections on the running workspace (no external peers) =="

# List ESTABLISHED TCP peers inside the running workspace. Prefer `ss`; fall
# back to a stdlib /proc/net/tcp parser (the slim image has no `ss`).
established="$(docker compose exec -T "$WORKSPACE_SVC" sh -c \
  'command -v ss >/dev/null 2>&1 && ss -H -tn state established 2>/dev/null || python3 - <<PY
import struct, socket
def rows(path, fam):
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        return
    for ln in lines:
        f = ln.split()
        if len(f) < 4 or f[3] != "01":  # 01 = ESTABLISHED
            continue
        raw, port = f[2].split(":")
        ip = socket.inet_ntop(fam, bytes.fromhex(raw)[::-1]) if fam==socket.AF_INET \
             else socket.inet_ntop(fam, struct.pack(">4I", *struct.unpack("<4I", bytes.fromhex(raw))))
        print("%s:%d" % (ip, int(port, 16)))
rows("/proc/net/tcp", socket.AF_INET)
rows("/proc/net/tcp6", socket.AF_INET6)
PY' 2>/dev/null || true)"

external="$(printf '%s\n' "$established" \
  | grep -vE '(^$|127\.0\.0\.1|::1|:8321($| )|:8321$)' \
  | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}|[0-9a-f:]+' \
  | grep -vE '^(127\.|::1$|0\.0\.0\.0$|172\.1[6-9]\.|172\.2[0-9]\.|172\.3[01]\.|10\.|192\.168\.)' \
  || true)"

if [ -z "$external" ]; then
  ok "no ESTABLISHED connection to an external (non-loopback, non-private-bridge) peer"
else
  note "peers seen (review — private-bridge/loopback are expected):"
  printf '%s\n' "$established" | sed 's/^/         /'
  bad "an ESTABLISHED connection to an external peer was found: $external"
fi

# ---------------------------------------------------------------------------
say ""
say "== Summary: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
