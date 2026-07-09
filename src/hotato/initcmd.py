"""``hotato init webhook``: scaffold a self-hostable webhook worker.

Generates a small, ready-to-deploy project that turns a voice platform's
call-ended webhook into a passive turn-taking regression monitor. The worker
verifies the webhook secret, then hands the payload to ``hotato ingest`` -- the
SAME composable primitive documented in docs/INGEST.md -- which fetches the
dual-channel recording READ-ONLY and scans it for CANDIDATE moments. It adds no
vendor call of its own.

The generated worker holds four hard invariants (enforced in the generated
``app.py`` and asserted in the generated ``tests/test_webhook_contract.py``):

  1. it never calls a platform config-mutation endpoint;
  2. it never labels intent or emits a verdict -- discovery only;
  3. it verifies the webhook secret BEFORE any parse, fetch, or scan;
  4. the recording fetch is read-only.

Templates ship in the wheel under ``hotato/templates/webhook`` and are rendered
per stack by literal ``{{TOKEN}}`` substitution (no template engine, stdlib
only). The per-stack signature-verification and event-detection code lives in
``templates/webhook/fragments`` so each shipped snippet is the verified one.
"""

from __future__ import annotations

import os
import stat
from importlib import resources
from typing import Optional

from . import __version__
from . import errors as _errors

__all__ = ["WEBHOOK_STACKS", "TARGETS", "InitError", "scaffold_webhook"]

# Only the stacks with a verified webhook + a read-only recording fetch (see
# docs/ADAPTER-STATUS.md and ingest.py). No unverified signature scheme ships.
WEBHOOK_STACKS = ("vapi", "retell", "twilio")
TARGETS = ("fastapi",)


class InitError(ValueError):
    """A usage / IO problem scaffolding a worker. Subclasses ``ValueError`` so
    the CLI maps it to exit 2 (never a crash)."""


# Per-stack values rendered into the shared templates. Kept honest: the event
# name, verify method, and header match the vendor's real webhook (see the
# fragments and docs/ADAPTER-STATUS.md).
_ENV_VARS = {
    "vapi": (
        "# --- Vapi ---\n"
        "# The shared secret you configured on the server URL (sent as X-Vapi-Secret).\n"
        "VAPI_WEBHOOK_SECRET=\n"
        "# Your Vapi private API key. Used ONLY by `hotato ingest` to fetch the\n"
        "# recording (read-only).\n"
        "VAPI_API_KEY="
    ),
    "retell": (
        "# --- Retell ---\n"
        "# Your Retell API key. Verifies X-Retell-Signature AND (via `hotato ingest`)\n"
        "# fetches the recording read-only.\n"
        "RETELL_API_KEY="
    ),
    "twilio": (
        "# --- Twilio ---\n"
        "# Account SID + Auth Token. The token verifies X-Twilio-Signature; both are\n"
        "# used by `hotato ingest` to fetch the recording read-only.\n"
        "TWILIO_ACCOUNT_SID=\n"
        "TWILIO_AUTH_TOKEN=\n"
        "# The exact public url Twilio posts to (scheme + host + path); required to\n"
        "# validate the request signature.\n"
        "TWILIO_WEBHOOK_URL="
    ),
}

_VALUES = {
    "vapi": {
        "STACK_TITLE": "Vapi",
        "EVENT_NAME": "end-of-call-report",
        "VERIFY_METHOD": "shared-secret",
        "SECRET_HEADER": "X-Vapi-Secret",
        "SECRET_ENV": "VAPI_WEBHOOK_SECRET",
    },
    "retell": {
        "STACK_TITLE": "Retell",
        "EVENT_NAME": "call_ended",
        "VERIFY_METHOD": "hmac-sha256",
        "SECRET_HEADER": "X-Retell-Signature",
        "SECRET_ENV": "RETELL_API_KEY",
    },
    "twilio": {
        "STACK_TITLE": "Twilio",
        "EVENT_NAME": "completed",
        "VERIFY_METHOD": "hmac-sha1",
        "SECRET_HEADER": "X-Twilio-Signature",
        "SECRET_ENV": "TWILIO_AUTH_TOKEN",
    },
}

# template file (in the wheel) -> destination path (in the generated project).
_FILES = {
    "README.md.tmpl": "README.md",
    "hotato.yaml.tmpl": "hotato.yaml",
    "app.py.tmpl": "app.py",
    "requirements.txt.tmpl": "requirements.txt",
    "Dockerfile.tmpl": "Dockerfile",
    "env.example.tmpl": ".env.example",
    "deploy.yml.tmpl": os.path.join(".github", "workflows", "deploy.yml"),
    "test_webhook_contract.py.tmpl": os.path.join("tests", "test_webhook_contract.py"),
}


def _template_text(*parts: str) -> str:
    return (
        resources.files("hotato")
        .joinpath("templates", "webhook", *parts)
        .read_text(encoding="utf-8")
    )


def _render(text: str, tokens: dict) -> str:
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _tokens(stack: str, target: str) -> dict:
    v = _VALUES[stack]
    return {
        "STACK": stack,
        "TARGET": target,
        "STACK_TITLE": v["STACK_TITLE"],
        "EVENT_NAME": v["EVENT_NAME"],
        "VERIFY_METHOD": v["VERIFY_METHOD"],
        "SECRET_HEADER": v["SECRET_HEADER"],
        "SECRET_ENV": v["SECRET_ENV"],
        "ENV_VARS_BLOCK": _ENV_VARS[stack],
        "VERSION": __version__,
        "GENERATED_BY": f"hotato init webhook --stack {stack} --target {target}",
        # The per-stack verified snippets, injected at module top level.
        "VERIFY_FUNCTION": _template_text("fragments", f"verify_{stack}.py").rstrip("\n"),
        "DETECT_FUNCTIONS": _template_text("fragments", f"events_{stack}.py").rstrip("\n"),
    }


def _write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    # Files 644 (the shipped website-packaging convention); keeps generated
    # projects tidy and predictable.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def scaffold_webhook(
    stack: str,
    target: str,
    out_dir: str,
    *,
    force: bool = False,
) -> dict:
    """Render the per-stack webhook worker into ``out_dir`` and return a result
    dict. Raises :class:`InitError` (-> exit 2) on an unknown stack/target or an
    existing file without ``force``. Writes nothing until every destination is
    clear (or ``force`` is set)."""
    stack = (stack or "").strip().lower()
    target = (target or "").strip().lower()
    if stack not in WEBHOOK_STACKS:
        raise InitError(
            f"unknown --stack {stack!r}; choose one of {', '.join(WEBHOOK_STACKS)}. "
            "These are the stacks with a verified webhook and a read-only "
            "recording fetch."
        )
    if target not in TARGETS:
        raise InitError(
            f"unknown --target {target!r}; choose one of {', '.join(TARGETS)}."
        )

    dests = {tmpl: os.path.join(out_dir, rel) for tmpl, rel in _FILES.items()}
    if not force:
        existing = [os.path.relpath(p, out_dir) for p in dests.values()
                    if os.path.exists(p)]
        if existing:
            raise InitError(
                f"{', '.join(sorted(existing))} already exist(s) under {out_dir!r}; "
                "pass --force to overwrite, or choose a fresh --out directory."
            )

    tokens = _tokens(stack, target)
    for tmpl, dest in dests.items():
        _write(dest, _render(_template_text(tmpl), tokens))

    files = sorted(os.path.relpath(p, out_dir) for p in dests.values())
    return {
        "tool": _errors.TOOL,
        "kind": "init-webhook",
        "stack": stack,
        "target": target,
        "out": out_dir,
        "files": files,
        "next": [
            f"cd {out_dir}",
            "cp .env.example .env   # fill in your secrets",
            "pip install -r requirements.txt",
            "pytest -q tests/test_webhook_contract.py   # the four invariants",
            "uvicorn app:app --reload   # POST /webhook ; GET /health",
        ],
    }


def render_text(result: dict) -> str:
    lines = [
        f"scaffolded a {result['stack']} webhook worker "
        f"({result['target']}) to {result['out']}",
        "",
        "files:",
    ]
    lines += [f"  {f}" for f in result["files"]]
    lines += [
        "",
        "It verifies the webhook secret, then delegates to `hotato ingest` "
        "(read-only fetch + candidate scan). It never mutates platform config "
        "and never labels intent; the four invariants are pinned in "
        "tests/test_webhook_contract.py.",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"]]
    return "\n".join(lines) + "\n"
