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

import os
import shlex
import stat
from importlib import resources

from . import __version__
from . import errors as _errors

__all__ = [
    "WEBHOOK_STACKS", "TARGETS", "InitError", "scaffold_webhook",
    "STARTER_STACKS", "scaffold_starter", "render_starter_text",
    "CI_SYSTEMS", "scaffold_ci", "render_ci_text",
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

STARTER_STACKS = ("vapi", "retell", "twilio", "livekit", "pipecat")

# Auto-pull: `hotato connect <stack>` + `hotato sweep --stack <stack>` fetch
# the recording for you (see capture.DUAL_PULL_STACKS / docs/CONNECT.md).
_STARTER_AUTO_PULL = ("vapi", "retell", "twilio")
# Capture-in-your-infra: no vendor recording endpoint; `hotato setup --stack
# <stack>` prints the two-track scaffold instead (docs/ADAPTER-STATUS.md).
_STARTER_CAPTURE_ONLY = ("livekit", "pipecat")

assert set(STARTER_STACKS) == set(_STARTER_AUTO_PULL) | set(_STARTER_CAPTURE_ONLY)

_STARTER_TITLES = {
    "vapi": "Vapi",
    "retell": "Retell",
    "twilio": "Twilio",
    "livekit": "LiveKit",
    "pipecat": "Pipecat",
}

# Credential env vars per stack -- the SAME names `hotato connect` / the
# webhook scaffold's _ENV_VARS use. Empty for the two capture-only stacks:
# hotato needs no credentials for them at all.
_STARTER_ENV_VARS = {
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
    return {
        "tool": _errors.TOOL,
        "kind": "init-starter",
        "stack": stack,
        "out": out_dir,
        "files": files,
        "auto_pull": auto_pull,
        "credential_env": list(_STARTER_ENV_VARS[stack]),
        "next": (
            [
                f"cd {shlex.quote(out_dir)}" if out_dir != "." else None,
                f"hotato connect {stack} --api-key YOUR_API_KEY",
                f"hotato sweep --stack {stack} --out hotato-sweep.html",
                "hotato contract create --from-candidate "
                "hotato-sweep.json#1 --expect yield --id refund-cutoff-001 "
                "--out contracts",
            ] if auto_pull else [
                f"cd {shlex.quote(out_dir)}" if out_dir != "." else None,
                f"hotato setup --stack {stack}",
                f"hotato inspect --stack {stack} --config agent.py",
                "hotato contract create --stereo call.wav --onset 42.18 "
                "--expect yield --id refund-cutoff-001 --out contracts",
            ]
        ),
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
