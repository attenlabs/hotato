"""``hotato init webhook`` / ``hotato init starter`` / ``hotato init ci``:
scaffolding for adding hotato to a voice-agent repository.

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

import json
import os
import re
import shlex
import stat
from importlib import resources

from . import __version__
from . import errors as _errors

try:  # stdlib on 3.11+; the >=3.9 floor keeps a text fallback for 3.9/3.10.
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised via monkeypatch below
    _tomllib = None

__all__ = [
    "WEBHOOK_STACKS", "TARGETS", "InitError", "scaffold_webhook",
    "STARTER_STACKS", "scaffold_starter", "render_starter_text",
    "CI_SYSTEMS", "scaffold_ci", "render_ci_text",
    "MCP_COMMAND", "register_agents", "render_agents_text",
    "agents_result_json",
    "FRAMEWORK_REGISTRY", "RECORDING_DIRS", "detect_frameworks",
    "locate_recordings", "choose_stack", "scaffold_auto", "render_auto_text",
    "auto_result_json",
]

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


def _as_posix(rel: str) -> str:
    """Normalize a native relative locator to ``/`` separators for the PUBLIC
    machine-JSON ``files`` list. Only the reported locator is normalized;
    ``os.path.join`` native paths are still used for every filesystem write, so
    a Windows run does the same I/O but emits the same portable JSON a POSIX
    run does (no ``\\`` leaks into the public contract)."""
    return rel.replace("\\", "/")


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

    files = sorted(_as_posix(os.path.relpath(p, out_dir)) for p in dests.values())
    return {
        "tool": _errors.TOOL,
        "kind": "init-webhook",
        "stack": stack,
        "target": target,
        "out": out_dir,
        "files": files,
        "next": [
            f"cd {shlex.quote(out_dir)}",
            "cp .env.example .env",
            "pip install -r requirements.txt",
            "pytest -q tests/test_webhook_contract.py",
            "uvicorn app:app --reload",
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
        "tests/test_webhook_contract.py. Fill in your secrets in .env, then it "
        "serves POST /webhook and GET /health.",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"]]
    return "\n".join(lines) + "\n"


# =============================================================================
# ``hotato init starter``: a whole-repo starter kit (CI gate + fixtures/ +
# contracts/ + reports/ + a stack-tuned hotato.yaml), instead of the single
# webhook worker above.
#
# The per-stack differences are drawn from the SAME source of truth as the
# rest of the CLI, never invented here: capture.STACKS (docs/ADAPTER-STATUS.md)
# splits into auto-pull stacks (Vapi/Retell/Twilio -- hotato fetches the
# recording itself once `hotato connect` holds a key) and capture-in-your-infra
# stacks (LiveKit/Pipecat -- no vendor recording API; `hotato setup` prints the
# two-track scaffold and you point hotato at the file your own deployment
# writes). A stack with no shipped connector would say so plainly and fall
# back to the generic path; today every STARTER_STACKS entry has one, so no
# such branch is reachable in this release (asserted below and in tests).
# =============================================================================

# `generic` first: the stack-agnostic default the guided first run points at
# (`hotato init starter --stack generic --out .`). It needs no vendor
# connector -- you point hotato at a two-channel WAV your own pipeline already
# records -- so it is the one starter stack that is deliberately NOT in
# capture.STACKS. The vendor stacks follow as the stack-tuned alternatives.
STARTER_STACKS = ("generic", "vapi", "retell", "twilio", "livekit", "pipecat")

# Auto-pull: `hotato connect <stack>` + `hotato sweep --stack <stack>` fetch
# the recording for you (see capture.DUAL_PULL_STACKS / docs/CONNECT.md).
_STARTER_AUTO_PULL = ("vapi", "retell", "twilio")
# Capture-in-your-infra: no vendor recording endpoint; `hotato setup --stack
# <stack>` prints the two-track scaffold instead (docs/ADAPTER-STATUS.md).
_STARTER_CAPTURE_ONLY = ("livekit", "pipecat")
# Stack-agnostic: no vendor connector at all. You bring a two-channel WAV your
# own deployment already writes; re-scaffold with a vendor stack for tuned
# config and one-command recording pulls.
_STARTER_GENERIC = ("generic",)

assert set(STARTER_STACKS) == (
    set(_STARTER_AUTO_PULL) | set(_STARTER_CAPTURE_ONLY) | set(_STARTER_GENERIC)
)

_STARTER_TITLES = {
    "generic": "any voice stack",
    "vapi": "Vapi",
    "retell": "Retell",
    "twilio": "Twilio",
    "livekit": "LiveKit",
    "pipecat": "Pipecat",
}

# Credential env vars per stack -- the SAME names `hotato connect` / the
# webhook scaffold's _ENV_VARS use. Empty for the capture-only and generic
# stacks: hotato needs no credentials for them at all.
_STARTER_ENV_VARS = {
    "generic": (),
    "vapi": ("VAPI_API_KEY",),
    "retell": ("RETELL_API_KEY",),
    "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
    "livekit": (),
    "pipecat": (),
}

_STARTER_FILES = (
    "hotato.yaml",
    "HOTATO.md",
    ".gitignore",
    os.path.join(".github", "workflows", "hotato-contracts.yml"),
    os.path.join("fixtures", "README.md"),
    os.path.join("fixtures", "scenarios", ".gitkeep"),
    os.path.join("fixtures", "audio", ".gitkeep"),
    os.path.join("contracts", "README.md"),
    os.path.join("contracts", ".gitkeep"),
    os.path.join("reports", "README.md"),
    os.path.join("reports", ".gitkeep"),
)


def _starter_hotato_yaml(stack: str) -> str:
    title = _STARTER_TITLES[stack]
    envs = _STARTER_ENV_VARS[stack]
    if stack in _STARTER_AUTO_PULL:
        env_list = ", ".join(envs)
        credentials = (
            "credentials:\n"
            f"  env: [{env_list}]\n"
            f"  # `hotato connect {stack}` stores these once, locally, mode 0600\n"
            "  # (~/.hotato/connections.json); or set the env var(s) directly.\n"
        )
        recording = (
            "recording:\n"
            "  access: auto-pull   # hotato fetches the recording itself via "
            "the vendor API\n"
            "  channels: dual\n"
        )
    elif stack in _STARTER_GENERIC:
        credentials = (
            "credentials:\n"
            "  env: []\n"
            "  # The generic starter needs no vendor credentials: point hotato\n"
            "  # at a two-channel WAV your own pipeline already records (caller\n"
            "  # on one channel, agent on the other). Re-scaffold with --stack\n"
            "  # vapi|retell|twilio|livekit|pipecat for stack-tuned config.\n"
        )
        recording = (
            "recording:\n"
            "  access: capture-in-your-infra   # score a two-channel WAV you "
            "already have\n"
            "  channels: dual\n"
        )
    else:
        credentials = (
            "credentials:\n"
            "  env: []\n"
            f"  # {title} has no vendor recording API hotato can pull from; no\n"
            "  # credentials are needed. Run `hotato setup --stack "
            f"{stack}` once for\n"
            "  # the two-track capture scaffold, then score the WAV your own\n"
            "  # deployment writes.\n"
        )
        recording = (
            "recording:\n"
            "  access: capture-in-your-infra   # see docs/ADAPTER-STATUS.md\n"
            "  channels: dual\n"
        )
    return (
        f"# hotato.yaml -- starter config for {title}.\n"
        "#\n"
        f"# Generated by `hotato init starter --stack {stack}`. Hotato is an\n"
        "# offline turn-taking regression tester: it never sits in the\n"
        f"# production audio path and never mutates {title} config.\n"
        "version: 1\n"
        f"stack: {stack}\n"
        "\n"
        f"{credentials}"
        "\n"
        f"{recording}"
        "\n"
        "fixtures:\n"
        "  scenarios_dir: fixtures/scenarios\n"
        "  audio_dir: fixtures/audio\n"
        "\n"
        "contracts:\n"
        "  dir: contracts\n"
        "\n"
        "reports:\n"
        "  dir: reports\n"
        "  formats: [json, html]\n"
        "\n"
        "ci:\n"
        "  workflow: .github/workflows/hotato-contracts.yml\n"
        "  junit: hotato.xml\n"
    )


def _starter_workflow_yaml(stack: str) -> str:
    title = _STARTER_TITLES[stack]
    lines = [
        "# hotato CI: verify failure contracts + regression fixtures for "
        "this repo.",
        "#",
        f"# Generated by `hotato init starter --stack {stack}`. Both gates "
        "below are",
        "# offline (no credentials needed) and are a no-op, never a "
        "failure, until",
        "# you have added a first contract or fixture -- an empty "
        "contracts/ or",
        "# fixtures/ directory is a normal starting state.",
        "#",
        "#   contracts/*.hotato                    -- `hotato contract "
        "create`,",
        "#                                             re-scored by "
        "`hotato contract verify`",
        "#   fixtures/scenarios + fixtures/audio   -- `hotato fixture "
        "create`,",
        "#                                             re-scored by "
        "`hotato run`",
        "",
        "name: hotato",
        "",
        "on:",
        "  push:",
        "  pull_request:",
        "  schedule:",
        '    - cron: "0 6 * * 1"   # weekly, catches drift with no code change',
        "",
        "jobs:",
        "  verify:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - uses: actions/setup-python@v5",
        "        with:",
        '          python-version: "3.12"',
        "      - name: Install hotato",
        "        run: python -m pip install hotato",
        "",
        "      - name: Verify failure contracts",
        "        run: |",
        '          if compgen -G "contracts/*.hotato" > /dev/null; then',
        "            hotato contract verify contracts --junit hotato.xml "
        "--format json > contracts-verify.json",
        "          else",
        '            echo "no contracts yet -- see contracts/README.md"',
        "          fi",
        "",
        "      - name: Run regression fixtures",
        "        run: |",
        '          if [ -d fixtures/scenarios ] && '
        '[ -n "$(ls -A fixtures/scenarios 2>/dev/null)" ]; then',
        "            hotato run --scenarios fixtures/scenarios --audio "
        "fixtures/audio --format json > fixtures-run.json",
        "          else",
        '            echo "no fixtures yet -- see fixtures/README.md"',
        "          fi",
        "",
        "      - name: Publish JUnit",
        "        if: always()",
        "        uses: actions/upload-artifact@v4",
        "        with:",
        "          name: hotato-contracts-junit",
        "          path: hotato.xml",
        "          if-no-files-found: ignore",
    ]
    if stack in _STARTER_AUTO_PULL:
        envs = _STARTER_ENV_VARS[stack]
        lines += [
            "",
            "  weekly-sweep:",
            "    # Candidate discovery only (never a verdict, never "
            "auto-labeled): a",
            f"    # passive sweep of recent {title} calls, ranked by "
            "hotato's own",
            "    # salience (docs/CONNECT.md). Disabled by default -- flip "
            "`if: false`",
            "    # to `if: true` once " + ", ".join(envs) + " " +
            ("is" if len(envs) == 1 else "are") +
            " set as repo secrets",
            "    # (Settings -> Secrets and variables -> Actions).",
            "    if: false",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: actions/setup-python@v5",
            "        with:",
            '          python-version: "3.12"',
            "      - run: python -m pip install hotato",
            f"      - name: Sweep recent {stack} calls",
            "        env:",
        ]
        lines += [f"          {e}: ${{{{ secrets.{e} }}}}" for e in envs]
        lines += [
            f"        run: hotato sweep --stack {stack} --format json "
            "--out hotato-sweep.json",
            "      - uses: actions/upload-artifact@v4",
            "        with:",
            "          name: hotato-weekly-sweep",
            "          path: hotato-sweep.json",
        ]
    elif stack in _STARTER_GENERIC:
        lines += [
            "",
            "  # The generic kit assumes no vendor recording API to sweep: "
            "point the",
            "  # gate at a two-channel WAV your own pipeline records, or "
            "re-scaffold",
            "  # with --stack vapi|retell|twilio to enable a weekly "
            "candidate-discovery",
            "  # sweep of recent calls.",
        ]
    else:
        lines += [
            "",
            f"  # {title} has no vendor recording API to sweep -- it is "
            "capture-in-",
            "  # your-infra. Run `hotato setup --stack " + stack + "` once "
            "for the",
            "  # two-track capture scaffold; there is no weekly-sweep job "
            "to enable.",
        ]
    return "\n".join(lines) + "\n"


def _starter_gitignore() -> str:
    return (
        "# hotato: local/pulled recordings and generated CI scratch.\n"
        "#\n"
        "# Fixture and contract audio clips ARE committed (they are the\n"
        "# pinned regression evidence, trimmed to a few seconds around the\n"
        "# event) -- only a raw/local recording outside those two paths is\n"
        "# excluded here.\n"
        "*.wav\n"
        "!fixtures/audio/*.wav\n"
        "!contracts/**/audio/*.wav\n"
        "\n"
        "hotato-sweep.json\n"
        "hotato-sweep.html\n"
        "hotato.xml\n"
        "contracts-verify.json\n"
        "fixtures-run.json\n"
        ".hotato-cache/\n"
        "\n"
        "/reports/*.html\n"
        "/reports/*.json\n"
        "!/reports/README.md\n"
        "!/reports/.gitkeep\n"
    )


def _starter_fixtures_readme() -> str:
    return (
        "# fixtures\n"
        "\n"
        "Regression fixtures: a real bad call moment, pinned. Each fixture "
        "is a\n"
        "label (`fixtures/scenarios/<id>.json`) plus a trimmed two-channel "
        "clip\n"
        "(`fixtures/audio/<id>.example.wav`), created and validated "
        "together by\n"
        "`hotato fixture create`, or promoted from a sweep/scan candidate "
        "with\n"
        "`hotato fixture promote`.\n"
        "\n"
        "Add one:\n"
        "\n"
        "    hotato fixture create --stereo bad-call.wav "
        "--id refund-interruption-001 --onset 42.18 --expect yield "
        "--out fixtures\n"
        "\n"
        "Run the battery:\n"
        "\n"
        "    hotato run --scenarios fixtures/scenarios --audio "
        "fixtures/audio\n"
        "\n"
        "`.github/workflows/hotato-contracts.yml` runs this on every push, "
        "every\n"
        "pull request, and weekly. Full walkthrough: docs/BAD-CALL-TO-CI.md "
        "in\n"
        "the hotato repo, or https://hotato.dev.\n"
        "\n"
        "Both subdirectories start empty (a `.gitkeep` each); an empty "
        "fixtures/\n"
        "directory is a normal starting state, not a failure.\n"
    )


def _starter_contracts_readme() -> str:
    return (
        "# contracts\n"
        "\n"
        "Portable failure contracts: one real call moment, its audio, "
        "frame-level\n"
        "timing evidence, an input-health report, a shareable card, and a "
        "CI\n"
        "pass/fail policy, bundled as `<id>.hotato/`.\n"
        "\n"
        "Add one:\n"
        "\n"
        "    hotato contract create --stereo bad-call.wav --onset 42.18 "
        "--expect yield --id refund-cutoff-001 --out contracts\n"
        "\n"
        "    # or, from a sweep/scan candidate:\n"
        "    hotato contract create --from-candidate hotato-sweep.json#1 "
        "--expect yield --id refund-cutoff-001 --out contracts\n"
        "\n"
        "Verify the battery (the CI gate):\n"
        "\n"
        "    hotato contract verify contracts --junit hotato.xml\n"
        "\n"
        "`.github/workflows/hotato-contracts.yml` runs this on every push, "
        "every\n"
        "pull request, and weekly, and fails the job on a regression. "
        "Bundle\n"
        "layout and full walkthrough: docs/CONTRACTS.md in the hotato "
        "repo, or\n"
        "https://hotato.dev.\n"
        "\n"
        "Hotato does not prove authorization, identity, compliance, or "
        "policy\n"
        "safety. It proves timing behavior against the explicit contract "
        "you\n"
        "wrote here.\n"
        "\n"
        "This directory starts empty (a `.gitkeep`); an empty contracts/\n"
        "directory is a normal starting state, not a failure.\n"
    )


def _starter_reports_readme() -> str:
    return (
        "# reports\n"
        "\n"
        "Where `hotato doctor`, `hotato report`, `hotato sweep`, and "
        "`hotato\n"
        "contract verify --html` write their self-contained HTML/JSON "
        "output by\n"
        "default (`reports/<name>.html`, `reports/<name>.json`). This is "
        "local/CI\n"
        "scratch (see `.gitignore`) for evidence you review, not a "
        "permanent\n"
        "record -- promote what matters into `contracts/` or `fixtures/` "
        "instead.\n"
        "\n"
        "This directory starts empty (a `.gitkeep`).\n"
    )


# Extra per-stack guidance for the two capture-in-your-infra stacks: WHERE the
# capture happens and WHERE turn-taking is actually configured, so the
# starter kit's HOTATO.md is not just "run setup" for these two. Facts here
# mirror the shipped scaffolds -- capture.py's _LIVEKIT_EGRESS_TEMPLATE /
# _PIPECAT_PROCESSOR_TEMPLATE (both `hotato setup --stack <name>` prints
# verbatim) and FIX-PLANS.md's Level 1 `hotato inspect` -- nothing new is
# invented here, only surfaced earlier in the adoption path.
_STARTER_CAPTURE_NOTES = {
    "livekit": (
        "LiveKit captures each participant's audio on its OWN track via "
        "Egress -- RoomComposite mixes both parties into one channel and "
        "cannot attribute overlap, so `hotato setup --stack livekit` prints "
        "the two-Track-egress scaffold (Python `livekit-api`, "
        "`TrackEgressRequest` + `DirectFileOutput`, one egress per party). "
        "Turn-taking itself is configured on `AgentSession(turn_handling="
        "TurnHandlingOptions(...))`: `turn_detection`, `endpointing`, and "
        "`interruption`. Read what your agent is ACTUALLY running before "
        "you touch any of it (static parse, never imported or executed):\n"
        "\n"
        "    hotato inspect --stack livekit --config agent.py\n"
    ),
    "pipecat": (
        "Pipecat captures both parties in-pipeline with a 2-channel "
        "`AudioBufferProcessor` (channel 0 = caller, channel 1 = agent) -- "
        "`hotato setup --stack pipecat` prints the exact processor + "
        "WAV-writer scaffold. Turn-taking itself lives on `PipelineTask`'s "
        "user-turn start/stop strategies (`VADUserTurnStartStrategy`, "
        "`MinWordsUserTurnStartStrategy`, `SpeechTimeoutUserTurnStopStrategy`,"
        " ...). Read what your bot is ACTUALLY running before you touch any "
        "of it (static parse, never imported or executed):\n"
        "\n"
        "    hotato inspect --stack pipecat --config bot.py\n"
    ),
}


def _starter_hotato_md(stack: str) -> str:
    title = _STARTER_TITLES[stack]
    if stack in _STARTER_AUTO_PULL:
        first_call = (
            f"    hotato connect {stack} --api-key YOUR_API_KEY\n"
            "    # (stored once, locally, mode 0600)\n"
            f"    hotato sweep --stack {stack} --out hotato-sweep.html\n"
            "    # open hotato-sweep.html, pick a real candidate moment, "
            "then:\n"
            "    hotato contract create --from-candidate "
            "hotato-sweep.json#1 --expect yield --id refund-cutoff-001 "
            "--out contracts\n"
        )
        capture_note = ""
    elif stack in _STARTER_GENERIC:
        first_call = (
            "    # point hotato at a two-channel WAV your pipeline already "
            "records\n"
            "    # (caller on one channel, agent on the other):\n"
            "    hotato contract create --stereo call.wav --onset 42.18 "
            "--expect yield --id refund-cutoff-001 --out contracts\n"
        )
        capture_note = (
            "\n"
            "## Tuning this kit to your stack\n"
            "\n"
            "This generic kit scores any two-channel recording. For "
            "stack-tuned\n"
            "config and one-command recording pulls, re-scaffold with a "
            "specific\n"
            "stack, for example `hotato init starter --stack vapi --out "
            "./hotato-vapi`\n"
            "(vapi, retell, twilio, livekit, or pipecat).\n"
        )
    else:
        first_call = (
            f"    hotato setup --stack {stack}\n"
            "    # prints the exact two-track capture scaffold\n"
            "    # once your deployment writes a two-channel WAV:\n"
            "    hotato contract create --stereo call.wav --onset 42.18 "
            "--expect yield --id refund-cutoff-001 --out contracts\n"
        )
        capture_note = (
            "\n"
            f"## Where capture and turn-taking config live for {title}\n"
            "\n"
            f"{_STARTER_CAPTURE_NOTES[stack]}"
        )
    return (
        f"# hotato starter kit ({title})\n"
        "\n"
        f"Generated by `hotato init starter --stack {stack}` on hotato "
        f"{__version__}.\n"
        "Hotato is an offline turn-taking regression tester for voice "
        "agents: it\n"
        "scores a recorded call and measures whether the agent stopped "
        "talking\n"
        "when the caller started (a yield), how long that took, and how "
        "many\n"
        "seconds both were talking at once (talk-over). It runs on the "
        "machine\n"
        "that invokes it and never sits in the production audio path.\n"
        "\n"
        "## What was added\n"
        "\n"
        "- `hotato.yaml` -- config skeleton for this stack.\n"
        "- `.gitignore` -- entries for local/pulled recordings; keeps "
        "pinned\n"
        "  fixture and contract clips committed. If you already have a\n"
        "  `.gitignore`, merge these lines by hand instead of overwriting "
        "it.\n"
        "- `.github/workflows/hotato-contracts.yml` -- CI: verifies\n"
        "  `contracts/` and `fixtures/` on push, pull request, and "
        "weekly; a\n"
        "  no-op (never a failure) until you have added your first one.\n"
        "- `fixtures/` -- regression fixtures (`hotato fixture create`); "
        "see\n"
        "  `fixtures/README.md`.\n"
        "- `contracts/` -- portable failure contracts (`hotato contract\n"
        "  create`); see `contracts/README.md`.\n"
        "- `reports/` -- local scratch output for HTML/JSON reports.\n"
        "\n"
        "## Get your first real call scored\n"
        "\n"
        f"{first_call}"
        f"{capture_note}"
        "\n"
        "## Next steps\n"
        "\n"
        "1. Turn your first bad call into a contract (or a fixture) and "
        "commit it.\n"
        "2. Push; the CI gate in `.github/workflows/hotato-contracts.yml`\n"
        "   verifies it on every pull request from here on.\n"
        "3. When you change a turn-taking setting, prove it: `hotato "
        "verify\n"
        "   --before before.json --after after.json`.\n"
        "\n"
        "Hotato does not infer intent: you label `yield`/`hold`; it "
        "measures\n"
        "timing. It does not prove authorization, identity, compliance, "
        "or\n"
        "policy safety. Docs: https://hotato.dev -- AGENTS.md in the "
        "hotato repo\n"
        "(agent-driven adoption recipe), docs/CONTRACTS.md, "
        "docs/BAD-CALL-TO-CI.md.\n"
    )


_STARTER_BUILDERS = {
    "hotato.yaml": lambda stack: _starter_hotato_yaml(stack),
    "HOTATO.md": lambda stack: _starter_hotato_md(stack),
    ".gitignore": lambda stack: _starter_gitignore(),
    os.path.join(".github", "workflows", "hotato-contracts.yml"):
        lambda stack: _starter_workflow_yaml(stack),
    os.path.join("fixtures", "README.md"): lambda stack: _starter_fixtures_readme(),
    os.path.join("fixtures", "scenarios", ".gitkeep"):
        lambda stack: "# populated by `hotato fixture create --out fixtures`.\n",
    os.path.join("fixtures", "audio", ".gitkeep"):
        lambda stack: "# populated by `hotato fixture create --out fixtures`.\n",
    os.path.join("contracts", "README.md"): lambda stack: _starter_contracts_readme(),
    os.path.join("contracts", ".gitkeep"):
        lambda stack: "# populated by `hotato contract create --out contracts`.\n",
    os.path.join("reports", "README.md"): lambda stack: _starter_reports_readme(),
    os.path.join("reports", ".gitkeep"):
        lambda stack: "# local/CI scratch; see reports/README.md.\n",
}


def scaffold_starter(stack: str, out_dir: str, *, force: bool = False) -> dict:
    """Render a whole-repo hotato starter kit into ``out_dir`` and return a
    result dict. Raises :class:`InitError` (-> exit 2) on an unknown stack or
    an existing destination file without ``force``. Writes nothing until
    every destination is clear (or ``force`` is set) -- the same all-or-
    nothing guarantee :func:`scaffold_webhook` gives.

    ``out_dir`` is typically the root of an EXISTING voice-agent repository
    (pass ``--out .``): the generated files are deliberately namespaced
    (``HOTATO.md`` rather than ``README.md``, a new
    ``.github/workflows/hotato-contracts.yml`` rather than the repo's own CI
    workflow) so a first run does not collide with files a real repo almost
    always already has. ``hotato.yaml`` and ``.gitignore`` are the two
    exceptions (there is exactly one canonical name for each); both are still
    refused-if-exists like every other generated file, never silently
    merged."""
    stack = (stack or "").strip().lower()
    if stack not in STARTER_STACKS:
        raise InitError(
            f"unknown --stack {stack!r}; choose one of {', '.join(STARTER_STACKS)}"
        )
    if not out_dir:
        raise InitError("--out DIR is required")

    dests = {rel: os.path.join(out_dir, rel) for rel in _STARTER_FILES}
    if not force:
        existing = [os.path.relpath(p, out_dir) for p in dests.values()
                    if os.path.exists(p)]
        if existing:
            raise InitError(
                f"{', '.join(sorted(existing))} already exist(s) under "
                f"{out_dir!r}; pass --force to overwrite, or choose a "
                "fresh --out directory"
            )

    for rel, dest in dests.items():
        _write(dest, _STARTER_BUILDERS[rel](stack))

    files = sorted(_as_posix(os.path.relpath(p, out_dir)) for p in dests.values())
    auto_pull = stack in _STARTER_AUTO_PULL
    cd = f"cd {shlex.quote(out_dir)}" if out_dir != "." else None
    if auto_pull:
        next_steps = [
            cd,
            f"hotato connect {stack} --api-key YOUR_API_KEY",
            f"hotato sweep --stack {stack} --out hotato-sweep.html",
            "hotato contract create --from-candidate "
            "hotato-sweep.json#1 --expect yield --id refund-cutoff-001 "
            "--out contracts",
        ]
    elif stack in _STARTER_GENERIC:
        next_steps = [
            cd,
            "hotato contract create --stereo call.wav --onset 42.18 "
            "--expect yield --id refund-cutoff-001 --out contracts",
            "hotato contract verify contracts --junit hotato.xml",
        ]
    else:
        next_steps = [
            cd,
            f"hotato setup --stack {stack}",
            f"hotato inspect --stack {stack} --config agent.py",
            "hotato contract create --stereo call.wav --onset 42.18 "
            "--expect yield --id refund-cutoff-001 --out contracts",
        ]
    return {
        "tool": _errors.TOOL,
        "kind": "init-starter",
        "stack": stack,
        "out": out_dir,
        "files": files,
        "auto_pull": auto_pull,
        "credential_env": list(_STARTER_ENV_VARS[stack]),
        "next": next_steps,
    }


def render_starter_text(result: dict) -> str:
    lines = [
        f"scaffolded a hotato starter kit ({result['stack']}) to "
        f"{result['out']}",
        "",
        "files:",
    ]
    lines += [f"  {f}" for f in result["files"]]
    lines += [
        "",
        "CI gate: .github/workflows/hotato-contracts.yml verifies "
        "contracts/ and",
        "fixtures/ on push, pull request, and weekly; a no-op until you "
        "add your",
        "first one. See HOTATO.md for the full next-steps walkthrough.",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"] if c]
    return "\n".join(lines) + "\n"


def starter_result_json(result: dict) -> dict:
    return dict(result)


# =============================================================================
# ``hotato init ci``: one canonical CI config per system, so the same
# exit-code gate the shipped GitHub Action and the starter kit's workflow run
# (verify contracts/, re-score fixtures/, fail the pipeline on non-zero exit)
# lands in GitLab CI, Jenkins, Azure Pipelines, or CircleCI in one command.
#
# The gate steps are the SAME two commands the starter workflow pins (and
# tests/test_init_starter.py cross-checks against the live CLI): `hotato
# contract verify contracts --junit hotato.xml` and `hotato run --scenarios
# fixtures/scenarios --audio fixtures/audio`. Each is guarded so an empty
# contracts/ or fixtures/ directory is a normal starting state, never a red
# pipeline; once one exists, hotato's non-zero exit fails the job. The
# generated config pins the CURRENT package version (the hotato that wrote
# it), so the gate is reproducible until the consumer bumps it on purpose.
# =============================================================================

CI_SYSTEMS = ("gitlab", "jenkins", "azure", "circleci")

_CI_TITLES = {
    "gitlab": "GitLab CI",
    "jenkins": "Jenkins",
    "azure": "Azure Pipelines",
    "circleci": "CircleCI",
}

# system -> the one canonical config filename that system reads.
_CI_FILES = {
    "gitlab": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "azure": "azure-pipelines.yml",
    "circleci": os.path.join(".circleci", "config.yml"),
}


def _ci_header_lines(system: str, comment: str) -> list:
    title = _CI_TITLES[system]
    return [
        f"{comment} hotato turn-taking gate ({title}): verifies contracts/ and"
        " re-scores",
        f"{comment} fixtures/ on every pipeline run; a regression exits"
        " non-zero and fails",
        f"{comment} the pipeline. Generated by `hotato init ci --system"
        f" {system}` on hotato",
        f"{comment} {__version__}. See docs/CI.md in the hotato repo, or"
        " https://hotato.dev.",
    ]


def _ci_gate_lines(hotato_bin: str = "hotato") -> tuple:
    """The two guarded gate scripts, as plain POSIX-shell lines (`ls` glob
    guards, not bashisms, so the same lines run under dash on a Jenkins
    agent and bash everywhere else). The two hotato commands are the same
    ones the starter workflow and docs/CI.md pin."""
    contracts = [
        "if ls contracts/*.hotato > /dev/null 2>&1; then",
        f"  {hotato_bin} contract verify contracts --junit hotato.xml "
        "--format json > contracts-verify.json",
        "else",
        '  echo "no contracts yet -- see docs/CONTRACTS.md in the hotato repo"',
        "fi",
    ]
    fixtures = [
        "if [ -d fixtures/scenarios ] && "
        '[ -n "$(ls -A fixtures/scenarios 2>/dev/null)" ]; then',
        f"  {hotato_bin} run --scenarios fixtures/scenarios --audio "
        "fixtures/audio --format json > fixtures-run.json",
        "else",
        '  echo "no fixtures yet -- see docs/BAD-CALL-TO-CI.md in the '
        'hotato repo"',
        "fi",
    ]
    return contracts, fixtures


def _ci_gitlab() -> str:
    contracts, fixtures = _ci_gate_lines()
    lines = _ci_header_lines("gitlab", "#") + [
        "hotato:",
        "  image: python:3.12",
        "  script:",
        f"    - pip install hotato=={__version__}",
        "    - |",
        *[f"      {ln}" for ln in contracts],
        "    - |",
        *[f"      {ln}" for ln in fixtures],
        "  artifacts:",
        "    when: always",
        "    paths:",
        "      - contracts-verify.json",
        "      - fixtures-run.json",
        "      - hotato.xml",
        "    reports:",
        "      junit: hotato.xml",
    ]
    return "\n".join(lines) + "\n"


def _ci_jenkins() -> str:
    # The venv keeps the install inside the workspace, so the same
    # Jenkinsfile runs on the python:3.12 docker agent AND on `agent any`
    # with a system Python (no --user/PATH coupling); `.hotato-venv/bin/...`
    # then needs no per-step activation.
    contracts, fixtures = _ci_gate_lines(".hotato-venv/bin/hotato")
    lines = _ci_header_lines("jenkins", "//") + [
        "pipeline {",
        "  // The docker agent needs the Docker Pipeline plugin; any agent "
        "with",
        "  // Python 3.10+ runs the same stages (swap in `agent any`).",
        "  agent { docker { image 'python:3.12' } }",
        "  stages {",
        "    stage('Install hotato') {",
        "      steps {",
        "        sh 'python -m venv .hotato-venv && "
        f".hotato-venv/bin/pip install hotato=={__version__}'",
        "      }",
        "    }",
        "    stage('Verify contracts') {",
        "      steps {",
        "        sh '''",
        *[f"          {ln}" for ln in contracts],
        "        '''",
        "      }",
        "    }",
        "    stage('Run fixtures') {",
        "      steps {",
        "        sh '''",
        *[f"          {ln}" for ln in fixtures],
        "        '''",
        "      }",
        "    }",
        "  }",
        "  post {",
        "    always {",
        "      junit allowEmptyResults: true, testResults: 'hotato.xml'",
        "      archiveArtifacts artifacts: 'contracts-verify.json, "
        "fixtures-run.json, hotato.xml', allowEmptyArchive: true",
        "    }",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _ci_azure() -> str:
    contracts, fixtures = _ci_gate_lines()
    lines = _ci_header_lines("azure", "#") + [
        "trigger:",
        "  branches:",
        "    include:",
        "      - '*'",
        "",
        "pool:",
        "  vmImage: ubuntu-latest",
        "",
        "steps:",
        "  - task: UsePythonVersion@0",
        "    inputs:",
        "      versionSpec: '3.12'",
        f"  - script: pip install hotato=={__version__}",
        "    displayName: Install hotato",
        "  - script: |",
        *[f"      {ln}" for ln in contracts],
        "    displayName: Verify contracts",
        "  - script: |",
        *[f"      {ln}" for ln in fixtures],
        "    displayName: Run fixtures",
        "  - task: PublishTestResults@2",
        "    condition: always()",
        "    inputs:",
        "      testResultsFiles: hotato.xml",
        "      testRunTitle: hotato",
        "  - task: CopyFiles@2",
        "    condition: always()",
        "    inputs:",
        "      contents: |",
        "        contracts-verify.json",
        "        fixtures-run.json",
        "        hotato.xml",
        "      targetFolder: $(Build.ArtifactStagingDirectory)",
        "  - task: PublishBuildArtifacts@1",
        "    condition: always()",
        "    inputs:",
        "      pathToPublish: $(Build.ArtifactStagingDirectory)",
        "      artifactName: hotato-reports",
    ]
    return "\n".join(lines) + "\n"


def _ci_circleci() -> str:
    contracts, fixtures = _ci_gate_lines()
    lines = _ci_header_lines("circleci", "#") + [
        "version: 2.1",
        "",
        "jobs:",
        "  hotato:",
        "    docker:",
        "      - image: cimg/python:3.12",
        "    steps:",
        "      - checkout",
        "      - run:",
        "          name: Install hotato",
        f"          command: pip install hotato=={__version__}",
        "      - run:",
        "          name: Verify contracts",
        "          command: |",
        *[f"            {ln}" for ln in contracts],
        "      - run:",
        "          name: Run fixtures",
        "          command: |",
        *[f"            {ln}" for ln in fixtures],
        "      - run:",
        "          name: Collect reports",
        "          when: always",
        "          command: |",
        "            mkdir -p hotato-ci-reports",
        "            for f in contracts-verify.json fixtures-run.json "
        "hotato.xml; do",
        '              if [ -f "$f" ]; then cp "$f" hotato-ci-reports/; fi',
        "            done",
        "      - store_test_results:",
        "          path: hotato-ci-reports",
        "      - store_artifacts:",
        "          path: hotato-ci-reports",
        "",
        "workflows:",
        "  hotato:",
        "    jobs:",
        "      - hotato",
    ]
    return "\n".join(lines) + "\n"


_CI_BUILDERS = {
    "gitlab": _ci_gitlab,
    "jenkins": _ci_jenkins,
    "azure": _ci_azure,
    "circleci": _ci_circleci,
}

assert set(_CI_BUILDERS) == set(CI_SYSTEMS) == set(_CI_FILES) == set(_CI_TITLES)


def scaffold_ci(system: str, out_dir: str, *, force: bool = False) -> dict:
    """Write the one canonical CI config for ``system`` into ``out_dir`` and
    return a result dict. Raises :class:`InitError` (-> exit 2) on an unknown
    system or an existing destination file without ``force`` -- the same
    refuse-then-overwrite convention as :func:`scaffold_webhook` and
    :func:`scaffold_starter`.

    ``out_dir`` is typically the root of an EXISTING voice-agent repository
    (the default ``--out .``): each system reads exactly one well-known path
    (.gitlab-ci.yml, Jenkinsfile, azure-pipelines.yml, .circleci/config.yml),
    so the file lands where that CI system looks for it."""
    system = (system or "").strip().lower()
    if system not in CI_SYSTEMS:
        raise InitError(
            f"unknown --system {system!r}; choose one of {', '.join(CI_SYSTEMS)}"
        )
    if not out_dir:
        raise InitError("--out DIR is required")

    rel = _CI_FILES[system]
    dest = os.path.join(out_dir, rel)
    if not force and os.path.exists(dest):
        raise InitError(
            f"{_as_posix(rel)} already exists under {out_dir!r}; pass --force "
            "to overwrite, or choose a fresh --out directory"
        )

    _write(dest, _CI_BUILDERS[system]())

    return {
        "tool": _errors.TOOL,
        "kind": "init-ci",
        "system": system,
        "out": out_dir,
        "files": [_as_posix(rel)],
        "pinned_version": __version__,
        "next": [
            "hotato contract create --stereo call.wav --onset 42.18 "
            "--expect yield --id refund-cutoff-001 --out contracts",
            "hotato contract verify contracts --junit hotato.xml",
            f"git add {shlex.quote(_as_posix(rel))} contracts",
        ],
    }


def render_ci_text(result: dict) -> str:
    title = _CI_TITLES[result["system"]]
    lines = [
        f"wrote the {title} turn-taking gate to {result['out']}",
        "",
        "files:",
    ]
    lines += [f"  {f}" for f in result["files"]]
    lines += [
        "",
        f"Every pipeline run installs hotato=={result['pinned_version']}, "
        "verifies contracts/,",
        "re-scores fixtures/, and publishes the JSON reports plus the JUnit "
        "file; a",
        "regression fails the pipeline. Each gate is a no-op until the first "
        "contract",
        "or fixture lands. See docs/CI.md in the hotato repo, or "
        "https://hotato.dev.",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"]]
    return "\n".join(lines) + "\n"


# =============================================================================
# ``hotato init --agents``: register hotato with the coding agents already
# configured in the current project.
#
# One command, run from the project root, that writes the SAME core-loop
# registration into every agent config surface the project already carries:
# an AGENTS.md section, a Claude Code skill (or CLAUDE.md section), a Cursor
# rule, and the project ``.mcp.json`` server entry. Everything is idempotent
# and additive:
#
#   * Markdown files the USER owns (AGENTS.md, CLAUDE.md, .cursorrules) get a
#     clearly delimited ``<!-- hotato:begin -->`` ... ``<!-- hotato:end -->``
#     block: created when absent, refreshed in place when present, and every
#     byte outside the markers is preserved untouched. A second run changes
#     nothing.
#   * Files hotato OWNS by path (.claude/skills/hotato/SKILL.md,
#     .cursor/rules/hotato.mdc) are written whole, and only while they carry
#     the managed-file marker line; a user-edited copy without the marker is
#     kept as-is, never overwritten.
#   * ``.mcp.json`` gains a ``mcpServers.hotato`` entry when the file exists;
#     every other key is preserved (JSON round-trip, two-space indent). An
#     unparseable file is kept untouched. When no ``.mcp.json`` exists, the
#     one-line stdio command is printed instead.
#
# The rendered core loop reuses the CLI's own ``_CORE_LOOP_STEPS`` tuple, so
# the registration can never drift from the GET STARTED block, ``describe``,
# AGENTS.md, and the README (the byte-identical-everywhere rule).
# =============================================================================

# The one-line stdio command every MCP client uses (docs/MCP.md; the --from
# form is load-bearing, `uvx hotato-mcp` alone fails).
MCP_COMMAND = 'uvx --from "hotato[mcp]" hotato-mcp'

# The same command as a project .mcp.json server entry.
_MCP_SERVER_ENTRY = {
    "command": "uvx",
    "args": ["--from", "hotato[mcp]", "hotato-mcp"],
}

# Delimiters for the block owned inside USER-owned markdown files. Everything
# between them is replaced on refresh; everything outside is never touched.
AGENTS_BLOCK_BEGIN = "<!-- hotato:begin -->"
AGENTS_BLOCK_END = "<!-- hotato:end -->"

# Marker carried by files hotato owns whole (SKILL.md, the Cursor rule). A
# copy without it has been taken over by the user and is left alone.
_MANAGED_MARK = "<!-- managed by `hotato init --agents`; re-run it to refresh -->"

_NEXT_STEP = "hotato start --demo"


def _core_loop_markdown() -> str:
    """The 5-step core loop as markdown, rendered from the CLI's own
    ``_CORE_LOOP_STEPS`` tuple (imported lazily; the CLI imports this module
    lazily too) so every registered surface stays byte-congruent with
    ``hotato --help`` and ``hotato describe``."""
    from .cli import _CORE_LOOP_STEPS

    return "\n".join(
        f"{i}. `{cmd}` -- {blurb}"
        for i, (cmd, blurb) in enumerate(_CORE_LOOP_STEPS, 1)
    )


def _registration_body() -> str:
    """The one registration text every surface carries: what hotato measures,
    the two-channel precondition, the core loop, the exit codes, and the
    machine contract."""
    return (
        "hotato scores the timing between the two channels of a recorded "
        "voice call\n"
        "(caller on one channel, agent on the other): did the agent stop "
        "talking when\n"
        "the caller took the floor, how fast, and how many seconds both were "
        "talking\n"
        "at once. Offline and deterministic; exit codes: 0 pass, 1 "
        "regression, 2 refuse.\n"
        "Scoring needs two separate channels; a mono or mixed export is NOT "
        "SCORABLE\n"
        "(exit 2), not scored.\n"
        "\n"
        "The core loop, first touch to a CI gate:\n"
        "\n"
        f"{_core_loop_markdown()}\n"
        "\n"
        "Machine contract: `hotato describe --format json` emits every "
        "command, flag,\n"
        "and exit code, generated from the CLI itself; read it before "
        "scripting, and\n"
        "do not hardcode the version or the command list.\n"
        f"MCP (local stdio): `{MCP_COMMAND}`."
    )


def _delimited_block() -> str:
    return (
        f"{AGENTS_BLOCK_BEGIN}\n"
        "## hotato: turn-taking regression checks for recorded voice calls\n"
        "\n"
        f"{_registration_body()}\n"
        f"{AGENTS_BLOCK_END}"
    )


def _skill_md() -> str:
    return (
        "---\n"
        "name: hotato\n"
        "description: Turn-taking regression checks for recorded voice "
        "calls. Use when a two-channel call recording needs a timing verdict "
        "(talk-over, slow yield, missed barge-in) or a caught failure should "
        "become a committed CI regression contract.\n"
        "---\n"
        "\n"
        f"{_MANAGED_MARK}\n"
        "\n"
        "# hotato\n"
        "\n"
        f"{_registration_body()}\n"
    )


def _cursor_rule() -> str:
    return (
        "---\n"
        "description: hotato turn-taking regression checks for recorded "
        "voice calls\n"
        "alwaysApply: false\n"
        "---\n"
        "\n"
        f"{_MANAGED_MARK}\n"
        "\n"
        f"{_registration_body()}\n"
    )


def _read_text_or_none(path: str):
    if not os.path.isfile(path):
        return None
    # open_regular: the path lives inside the user's project dir, so the read
    # is FIFO-guarded like every other externally supplied path.
    with _errors.open_regular(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # newline="": the written bytes are exactly the composed "\n" text on
    # every OS, so a second run compares byte-equal and rewrites nothing.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _upsert_block(existing, *, created_heading: str = "") -> str:
    """Return the file content with the delimited hotato block inserted
    (absent) or refreshed in place (present). Every byte outside the markers
    is preserved. A file with a begin marker but no end marker is refused
    (:class:`InitError` -> exit 2) rather than guessed at."""
    block = _delimited_block()
    if existing is None:
        head = f"{created_heading}\n\n" if created_heading else ""
        return f"{head}{block}\n"
    if AGENTS_BLOCK_BEGIN in existing:
        pre, rest = existing.split(AGENTS_BLOCK_BEGIN, 1)
        if AGENTS_BLOCK_END not in rest:
            raise InitError(
                f"found {AGENTS_BLOCK_BEGIN!r} without a matching "
                f"{AGENTS_BLOCK_END!r}; restore the end marker (or delete "
                "the block) and re-run"
            )
        _, post = rest.split(AGENTS_BLOCK_END, 1)
        return pre + block + post
    sep = "" if existing == "" else existing.rstrip("\n") + "\n\n"
    return f"{sep}{block}\n"


def _upsert_managed_markdown(path: str, rel: str, surface: str,
                             *, created_heading: str = "") -> dict:
    existing = _read_text_or_none(path)
    new = _upsert_block(existing, created_heading=created_heading)
    if existing is None:
        action = "created"
    elif new != existing:
        action = "updated"
    else:
        action = "unchanged"
    if action != "unchanged":
        _write_text(path, new)
    return {"surface": surface, "path": rel, "action": action}


def _write_owned_file(path: str, rel: str, surface: str, text: str) -> dict:
    existing = _read_text_or_none(path)
    if existing is None:
        action = "created"
    elif _MANAGED_MARK not in existing:
        # The user took this file over; keep every byte.
        return {"surface": surface, "path": rel, "action": "kept"}
    elif existing != text:
        action = "updated"
    else:
        action = "unchanged"
    if action != "unchanged":
        _write_text(path, text)
    return {"surface": surface, "path": rel, "action": action}


def _upsert_mcp_json(path: str, rel: str) -> dict:
    import json

    raw = _read_text_or_none(path)
    if raw is None:
        return {"surface": "mcp", "path": rel, "action": "absent"}
    try:
        doc = json.loads(raw)
    except ValueError:
        # Unparseable: keep the user's bytes; the printed one-line command
        # still carries the config.
        return {"surface": "mcp", "path": rel, "action": "kept"}
    if not isinstance(doc, dict):
        return {"surface": "mcp", "path": rel, "action": "kept"}
    servers = doc.get("mcpServers")
    if servers is None:
        servers = {}
        doc["mcpServers"] = servers
    if not isinstance(servers, dict):
        return {"surface": "mcp", "path": rel, "action": "kept"}
    if servers.get("hotato") == _MCP_SERVER_ENTRY:
        return {"surface": "mcp", "path": rel, "action": "unchanged"}
    action = "updated" if "hotato" in servers else "created"
    servers["hotato"] = dict(_MCP_SERVER_ENTRY)
    _write_text(path, json.dumps(doc, indent=2) + "\n")
    return {"surface": "mcp", "path": rel, "action": action}


def register_agents(root: str = ".") -> dict:
    """Register hotato with every agent config surface present under
    ``root`` (the current project directory) and return a result dict.

    Always writes/refreshes the AGENTS.md hotato block. Writes the Claude
    Code skill when ``.claude/`` exists (or a CLAUDE.md block when only
    CLAUDE.md does), the Cursor rule when ``.cursor/`` exists (or a
    .cursorrules block when only that file does), and the ``mcpServers``
    entry when ``.mcp.json`` exists. Idempotent: a second run reports every
    surface ``unchanged`` and rewrites nothing."""
    root = root or "."
    if not os.path.isdir(root):
        raise InitError(f"project directory {root!r} does not exist")

    surfaces = [
        _upsert_managed_markdown(
            os.path.join(root, "AGENTS.md"), "AGENTS.md", "agents-md",
            created_heading="# AGENTS.md",
        )
    ]

    if os.path.isdir(os.path.join(root, ".claude")):
        rel = _as_posix(os.path.join(".claude", "skills", "hotato", "SKILL.md"))
        surfaces.append(_write_owned_file(
            os.path.join(root, ".claude", "skills", "hotato", "SKILL.md"),
            rel, "claude-skill", _skill_md(),
        ))
    elif os.path.isfile(os.path.join(root, "CLAUDE.md")):
        surfaces.append(_upsert_managed_markdown(
            os.path.join(root, "CLAUDE.md"), "CLAUDE.md", "claude-md",
        ))

    if os.path.isdir(os.path.join(root, ".cursor")):
        rel = _as_posix(os.path.join(".cursor", "rules", "hotato.mdc"))
        surfaces.append(_write_owned_file(
            os.path.join(root, ".cursor", "rules", "hotato.mdc"),
            rel, "cursor-rule", _cursor_rule(),
        ))
    elif os.path.isfile(os.path.join(root, ".cursorrules")):
        surfaces.append(_upsert_managed_markdown(
            os.path.join(root, ".cursorrules"), ".cursorrules", "cursor-rules-file",
        ))

    surfaces.append(_upsert_mcp_json(os.path.join(root, ".mcp.json"), ".mcp.json"))

    return {
        "tool": _errors.TOOL,
        "kind": "init-agents",
        "root": root,
        "surfaces": surfaces,
        "mcp_command": MCP_COMMAND,
        "next": [_NEXT_STEP],
    }


_AGENTS_ACTION_TEXT = {
    "created": "wrote the hotato registration",
    "updated": "updated the hotato registration",
    "unchanged": "already current",
    "kept": "kept as-is (user-owned copy)",
}


def render_agents_text(result: dict) -> str:
    written = [s for s in result["surfaces"] if s["action"] != "absent"]
    lines = ["hotato init --agents: agent surfaces in "
             f"{result['root']}", ""]
    width = max(len(s["path"]) for s in written)
    for s in written:
        lines.append(f"  {s['path'].ljust(width)}  {_AGENTS_ACTION_TEXT[s['action']]}")
    lines += [
        "",
        f"MCP (any client, stdio): {result['mcp_command']}",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"]]
    return "\n".join(lines) + "\n"


def agents_result_json(result: dict) -> dict:
    return dict(result)


# =============================================================================
# ``hotato init --auto``: zero-config onboarding.
#
# One command, run from the root of an existing voice-agent repo, that (1)
# READ-ONLY inspects the project's declared dependencies for a known
# voice-agent framework, (2) locates any call recordings already committed,
# and (3) hands off to :func:`scaffold_starter` for the framework it found,
# pre-tuning the starter kit (hotato.yaml + the CI gate) to that stack and
# printing a first-baseline next step. It refuses cleanly (:class:`InitError`
# -> exit 2, with the manual path) when it finds neither a framework nor a
# recording, so a wrong guess is never scaffolded silently.
#
# Detection never imports or executes project code: pyproject.toml is parsed
# with the stdlib ``tomllib`` (a conservative text scan on the 3.9/3.10 floor),
# requirements.txt line by line, and package.json as JSON -- a static read of
# the SAME declared dependencies a human would read. Every match is reported
# with its evidence (the file and the dependency name), and the chosen stack is
# always one of :data:`STARTER_STACKS`, so `init --auto` can only ever produce
# a kit `init starter` could have produced by hand.
# =============================================================================

# Declared-dependency files, inspected in this fixed order (read-only).
_DEP_FILES = ("pyproject.toml", "requirements.txt", "package.json")

# Directories a recorded call commonly lands in, scanned for ``*.wav`` (plus any
# ``*.wav`` directly in the project root). Read-only; heavy vendored trees are
# pruned from the walk (see :data:`_SKIP_DIRS`).
RECORDING_DIRS = ("recordings", "logs", "calls", "audio", "call-recordings")
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "env",
    "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
}
_RECORDINGS_SCAN_CAP = 1000   # bound the walk on a pathological tree
_RECORDINGS_SAMPLE_CAP = 10   # how many paths the public result lists

# Known voice-agent packages -> (starter stack, framework family). Keys are
# normalized the same way :func:`_normalize_dep` normalizes a scanned name
# (PEP 503 for PyPI names; npm scoped names kept verbatim, lowercased). A
# vendor with a stack-tuned starter maps to that stack; a mono/mixed vendor
# with no dedicated starter maps to `generic` (the bring-your-own-WAV kit),
# which is honest: hotato scores its two-channel recording without a tuned
# connector. Every value's stack is asserted to be a real STARTER_STACKS entry.
FRAMEWORK_REGISTRY = {
    # --- PyPI ---
    "vapi": ("vapi", "vapi"),
    "vapi-python": ("vapi", "vapi"),
    "vapi-server-sdk": ("vapi", "vapi"),
    "retell-sdk": ("retell", "retell"),
    "retell": ("retell", "retell"),
    "twilio": ("twilio", "twilio"),
    "livekit": ("livekit", "livekit"),
    "livekit-agents": ("livekit", "livekit"),
    "livekit-api": ("livekit", "livekit"),
    "pipecat-ai": ("pipecat", "pipecat"),
    "pipecat": ("pipecat", "pipecat"),
    "elevenlabs": ("generic", "elevenlabs"),
    "synthflow": ("generic", "synthflow"),
    "cartesia": ("generic", "cartesia"),
    # --- npm ---
    "@vapi-ai/server-sdk": ("vapi", "vapi"),
    "@vapi-ai/web": ("vapi", "vapi"),
    "retell-client-js-sdk": ("retell", "retell"),
    "livekit-client": ("livekit", "livekit"),
    "@livekit/agents": ("livekit", "livekit"),
    "@livekit/rtc-node": ("livekit", "livekit"),
    "@elevenlabs/elevenlabs-js": ("generic", "elevenlabs"),
}

# Deterministic stack pick when a repo pulls in more than one framework: a
# stack-tuned vendor wins over the generic kit, earlier entries win over later.
_STACK_PRIORITY = ("vapi", "retell", "twilio", "livekit", "pipecat", "generic")

assert {stack for stack, _family in FRAMEWORK_REGISTRY.values()} <= set(STARTER_STACKS)
assert set(_STACK_PRIORITY) == set(STARTER_STACKS)


def _normalize_dep(name: str) -> str:
    """Normalize a dependency name for registry lookup. npm scoped names
    (``@scope/pkg``) are kept verbatim (lowercased); everything else gets PEP
    503 normalization (lowercase, runs of ``-_.`` collapsed to a single ``-``)
    so ``LiveKit_Agents`` and ``livekit-agents`` match the same entry."""
    n = name.strip().strip("\"'").lower()
    if n.startswith("@"):
        return n
    return re.sub(r"[-_.]+", "-", n)


def _req_name(spec: str) -> str:
    """The leading distribution name of a requirement/spec string, e.g.
    ``vapi>=1.0 ; python_version>='3.9'`` -> ``vapi``. Returns ``""`` for an
    editable/URL/option line (``-e ...``, ``git+https://...`` still yields
    ``git`` which simply never matches the registry)."""
    m = re.match(r"\s*([A-Za-z0-9@][A-Za-z0-9._/-]*)", spec)
    return m.group(1) if m else ""


def _loose_tokens(text: str) -> set:
    """Framework-name candidates from raw config text, used only when a
    structured parse is unavailable (the 3.9/3.10 tomllib floor) or fails.
    Pulls the leading name out of every quoted string (dependency specs) and
    every ``name =`` table key; spurious tokens are harmless because the caller
    only keeps names that hit the registry."""
    names = set()
    for m in re.finditer(r"[\"']([^\"']+)[\"']", text):
        n = _req_name(m.group(1))
        if n:
            names.add(_normalize_dep(n))
    for m in re.finditer(r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", text):
        names.add(_normalize_dep(m.group(1)))
    return names


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _deps_from_pyproject(text: str) -> set:
    if _tomllib is not None:
        try:
            doc = _tomllib.loads(text)
        except Exception:
            doc = None
        if isinstance(doc, dict):
            return _deps_from_pyproject_doc(doc)
    return _loose_tokens(text)


def _deps_from_pyproject_doc(doc: dict) -> set:
    names = set()
    project = doc.get("project")
    if isinstance(project, dict):
        for spec in _as_list(project.get("dependencies")):
            n = _req_name(str(spec))
            if n:
                names.add(_normalize_dep(n))
        opt = project.get("optional-dependencies")
        if isinstance(opt, dict):
            for group in opt.values():
                for spec in _as_list(group):
                    n = _req_name(str(spec))
                    if n:
                        names.add(_normalize_dep(n))
    # PEP 735 top-level [dependency-groups].
    groups = doc.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            for spec in _as_list(group):
                n = _req_name(str(spec))
                if n:
                    names.add(_normalize_dep(n))
    # Poetry: [tool.poetry.dependencies] table rows (name = "^x").
    tool = doc.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            deps = poetry.get("dependencies")
            if isinstance(deps, dict):
                for name in deps:
                    if name.lower() != "python":
                        names.add(_normalize_dep(name))
    return names


def _deps_from_requirements(text: str) -> set:
    names = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()
        n = _req_name(line)
        if n:
            names.add(_normalize_dep(n))
    return names


def _deps_from_package_json(text: str) -> set:
    try:
        doc = json.loads(text)
    except ValueError:
        return _loose_tokens(text)
    if not isinstance(doc, dict):
        return set()
    names = set()
    for key in ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies"):
        section = doc.get(key)
        if isinstance(section, dict):
            for name in section:
                names.add(_normalize_dep(name))
    return names


_DEP_PARSERS = {
    "pyproject.toml": _deps_from_pyproject,
    "requirements.txt": _deps_from_requirements,
    "package.json": _deps_from_package_json,
}


def detect_frameworks(root: str = ".") -> list:
    """Static, read-only scan of ``root``'s declared dependencies for known
    voice-agent frameworks. Returns one evidence dict per matched dependency
    -- ``{"file", "dependency", "framework", "stack"}`` -- in a deterministic
    order (files in :data:`_DEP_FILES` order, dependencies sorted). Never
    imports or executes project code."""
    detections = []
    for fname in _DEP_FILES:
        text = _read_text_or_none(os.path.join(root, fname))
        if text is None:
            continue
        for dep in sorted(_DEP_PARSERS[fname](text)):
            entry = FRAMEWORK_REGISTRY.get(dep)
            if entry:
                stack, family = entry
                detections.append({
                    "file": fname, "dependency": dep,
                    "framework": family, "stack": stack,
                })
    return detections


def _iter_wav_paths(base: str) -> list:
    """Every ``*.wav`` under ``base``, sorted, heavy trees pruned, capped."""
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
        )
        for fn in sorted(filenames):
            if fn.lower().endswith(".wav"):
                out.append(os.path.join(dirpath, fn))
                if len(out) >= _RECORDINGS_SCAN_CAP:
                    return out
    return out


def _root_wavs(root: str) -> list:
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    return [
        os.path.join(root, e) for e in entries
        if e.lower().endswith(".wav") and os.path.isfile(os.path.join(root, e))
    ]


def locate_recordings(root: str = ".") -> dict:
    """Read-only scan for committed call recordings under ``root``: every
    ``*.wav`` in :data:`RECORDING_DIRS` (recursively) plus any directly in the
    root. Returns ``{"dirs": [{"dir", "count"}], "files": [...capped],
    "total": int}`` with all paths ``/``-normalized relative to ``root`` and
    sorted, so the result is identical on every platform and run."""
    dir_counts = {}
    all_files = set()
    for d in RECORDING_DIRS:
        base = os.path.join(root, d)
        if os.path.isdir(base):
            found = [
                _as_posix(os.path.relpath(p, root)) for p in _iter_wav_paths(base)
            ]
            if found:
                dir_counts[d] = len(found)
                all_files.update(found)
    all_files.update(
        _as_posix(os.path.relpath(p, root)) for p in _root_wavs(root)
    )
    files = sorted(all_files)
    return {
        "dirs": [{"dir": d, "count": dir_counts[d]} for d in sorted(dir_counts)],
        "files": files[:_RECORDINGS_SAMPLE_CAP],
        "total": len(files),
    }


def choose_stack(detections: list):
    """Pick the single starter stack (and its framework family) for a list of
    detections, deterministically: a stack-tuned vendor beats the generic kit,
    then earlier :data:`_STACK_PRIORITY` entries, then file/dependency order.
    Returns ``(stack, family)`` or ``None`` for an empty list."""
    if not detections:
        return None
    ranked = sorted(
        detections,
        key=lambda d: (_STACK_PRIORITY.index(d["stack"]), d["file"], d["dependency"]),
    )
    return ranked[0]["stack"], ranked[0]["framework"]


def _auto_refusal(root: str) -> "InitError":
    dirs = ", ".join(f"{d}/" for d in RECORDING_DIRS)
    return InitError(
        "could not auto-detect a voice-agent framework or any call recordings "
        f"under {root!r}. Detection is read-only: it reads pyproject.toml, "
        "requirements.txt, and package.json dependencies, plus .wav files under "
        f"{dirs}. Name your stack to scaffold the same kit by hand:\n"
        "  hotato init starter --stack generic --out .   # score any two-channel WAV\n"
        "  hotato init starter --stack vapi    --out .   # or retell|twilio|livekit|pipecat"
    )


def scaffold_auto(root: str, out_dir: str, *, force: bool = False) -> dict:
    """Auto-detect the stack under ``root`` and render the matching starter kit
    into ``out_dir`` (typically the same directory). Raises :class:`InitError`
    (-> exit 2) when nothing is detected (with the manual path), when the stack
    is somehow unknown, or -- via :func:`scaffold_starter` -- when a destination
    file already exists without ``force``. Read-only detection runs BEFORE any
    write, and the scaffold itself is all-or-nothing, so a refusal leaves the
    project untouched."""
    root = root or "."
    if not os.path.isdir(root):
        raise InitError(f"project directory {root!r} does not exist")
    out_dir = out_dir or "."

    detections = detect_frameworks(root)
    recordings = locate_recordings(root)
    if not detections and recordings["total"] == 0:
        raise _auto_refusal(root)

    if detections:
        stack, framework = choose_stack(detections)
    else:
        # Recordings but no declared framework: the bring-your-own-WAV kit
        # scores exactly what was found, with no fabricated vendor claim.
        stack, framework = "generic", None

    scaffolded = scaffold_starter(stack, out_dir, force=force)

    first_wav = recordings["files"][0] if recordings["files"] else None
    baseline = (
        f"hotato investigate {shlex.quote(first_wav)}" if first_wav
        else "hotato start --demo"
    )
    # The gate command, GUARDED exactly like the CI job this scaffold generates
    # (.github/workflows/hotato-contracts.yml): a freshly scaffolded contracts/ is
    # empty, and a bare `hotato contract verify contracts` on an empty directory is
    # a usage error (exit 2) -- so printing it unguarded errors on its own scaffold.
    # The guard makes the empty scaffold a clean no-op (exit 0), then re-scores the
    # first contract once it exists (preserving verify's real 0/1 exit), matching
    # the CI job's "empty contracts/ is a normal starting state, a no-op, never a
    # failure". Wrapped in `sh -c` so the empty-glob guard is shell-agnostic (a
    # user's zsh `nomatch` never aborts it before the else branch runs).
    verify = (
        "sh -c 'if ls contracts/*.hotato >/dev/null 2>&1; then "
        "hotato contract verify contracts --junit hotato.xml; "
        "else echo \"no contracts yet -- add your first with the baseline "
        "above, then this gate runs\"; fi'"
    )
    if out_dir != ".":
        verify = f"cd {shlex.quote(out_dir)} && {verify}"
    next_steps = [baseline, verify]

    return {
        "tool": _errors.TOOL,
        "kind": "init-auto",
        "root": root,
        "out": out_dir,
        "detected": {
            "framework": framework,
            "stack": stack,
            "evidence": detections,
        },
        "recordings": recordings,
        "stack": stack,
        "auto_pull": scaffolded["auto_pull"],
        "credential_env": scaffolded["credential_env"],
        "files": scaffolded["files"],
        "next": next_steps,
    }


def render_auto_text(result: dict) -> str:
    det = result["detected"]
    rec = result["recordings"]
    if det["framework"]:
        sources = ", ".join(sorted({e["file"] for e in det["evidence"]}))
        head = (
            f"detected {det['framework']} in {sources}; scaffolded the "
            f"{result['stack']} starter kit to {result['out']}"
        )
    else:
        head = (
            f"found {rec['total']} call recording(s); scaffolded the "
            f"{result['stack']} starter kit to {result['out']}"
        )
    lines = [head, ""]
    if rec["total"]:
        sample = f" (e.g. {rec['files'][0]})" if rec["files"] else ""
        lines += [f"recordings: {rec['total']} found{sample}", ""]
    lines.append("files:")
    lines += [f"  {f}" for f in result["files"]]
    lines += [
        "",
        "CI gate: .github/workflows/hotato-contracts.yml verifies contracts/ "
        "and fixtures/",
        "on push, pull request, and weekly; a no-op until you add your first "
        "one.",
        "",
        "next:",
    ]
    lines += [f"  {c}" for c in result["next"]]
    return "\n".join(lines) + "\n"


def auto_result_json(result: dict) -> dict:
    return dict(result)
