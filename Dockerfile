# hotato self-host workspace — the team workspace (`hotato serve`) in a container.
#
# Multi-stage, slim Python base, non-root, installs hotato FROM THE LOCAL SOURCE
# (pip install .). The core is stdlib-only (zero runtime dependencies), so the
# default image makes no external calls at run time. Optional extras are opt-in
# build args:
#
#   docker build -t hotato-selfhost .                          # default: core only
#   docker build --build-arg WITH_TRANSCRIBE=1 -t hotato .     # + local faster-whisper ASR
#   docker build --build-arg WITH_SIGN=1 -t hotato .           # + Ed25519 signing (cryptography)
#
# WITH_TRANSCRIBE / WITH_SIGN are the two extras that make sense INSIDE the
# workspace container (a local ASR pass; signing evidence). The heavier live-capture
# and diarization extras run in your own pipeline, not here, so they are not built in.
#
# The container serves on 0.0.0.0:8321 BY NECESSITY (a published port needs the
# process to bind the container's interface, not loopback); privacy comes from the
# compose port mapping (127.0.0.1:8321:8321) — see docs/SELF-HOST.md. The
# non-loopback-bind warning the server prints at start is expected in-container.

# ---------------------------------------------------------------------------
# Stage 1 — build a venv with hotato + the selected extras installed
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ARG WITH_TRANSCRIBE=0
ARG WITH_SIGN=0

WORKDIR /src

# Only what the wheel build needs (package data is declared in pyproject).
COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip "setuptools>=77" wheel \
 && EXTRAS="" \
 && if [ "$WITH_TRANSCRIBE" = "1" ]; then EXTRAS="${EXTRAS:+$EXTRAS,}transcribe"; fi \
 && if [ "$WITH_SIGN" = "1" ]; then EXTRAS="${EXTRAS:+$EXTRAS,}sign"; fi \
 && if [ -n "$EXTRAS" ]; then SPEC=".[$EXTRAS]"; else SPEC="."; fi \
 && echo "hotato: installing spec $SPEC" \
 && pip install --no-cache-dir "$SPEC"

# ---------------------------------------------------------------------------
# Stage 2 — slim runtime, non-root, no build tooling
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# A fixed, non-root uid/gid so host bind-mount permissions are predictable.
ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd -g "$APP_GID" hotato \
 && useradd -u "$APP_UID" -g "$APP_GID" -m -s /usr/sbin/nologin hotato

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOTATO_REGISTRY=/data \
    HOTATO_WORKSPACE=default \
    HOTATO_SERVE_PORT=8321

# Deploy helpers (entrypoint, healthcheck, demo seeder). Not part of the package.
COPY deploy/entrypoint.sh deploy/healthcheck.py deploy/seed-demo.py /opt/hotato-deploy/
RUN chmod 0755 /opt/hotato-deploy/entrypoint.sh \
 && chmod 0755 /opt/hotato-deploy/healthcheck.py /opt/hotato-deploy/seed-demo.py

# The registry + evidence volume, owned by the non-root user so a FRESH named
# volume inherits that ownership on first mount.
RUN mkdir -p /data && chown -R hotato:hotato /data

USER hotato
WORKDIR /data
VOLUME ["/data"]

EXPOSE 8321

# Authenticated liveness: resolve the bearer token (secret / env / generated) and
# confirm a view returns 200 over loopback. Exec form — no shell, no piping a
# download into an interpreter.
HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=5 \
    CMD ["python3", "/opt/hotato-deploy/healthcheck.py"]

# The entrypoint injects the token flag (from a secret / env) without putting the
# secret on the command line, then execs the CMD below.
ENTRYPOINT ["/opt/hotato-deploy/entrypoint.sh"]
CMD ["hotato", "serve", "--workspace", "default", "--host", "0.0.0.0", "--port", "8321", "--registry", "/data"]
